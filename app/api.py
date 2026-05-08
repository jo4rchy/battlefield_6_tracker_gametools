"""FastAPI app — exposes TRN-compatible endpoints backed by gametools.

Flow for /profile:
    1. fetch gametools stats + profile (rank metadata)
    2. build the TRN-shape profile via converter.build_trn_profile
    3. generate update_hash from raw gametools counters
    4. upsert_profile_with_delta:
       * first-seen user              -> save TRN profile + save full-career first match
       * existing user, hash changed  -> save TRN match (TRN-profile subtraction),
                                          overwrite stored TRN profile
       * existing user, hash same     -> no-op
    5. return the TRN profile (+ deltaInfo describing what happened)

Background auto-refresh:
    On startup a background asyncio task kicks off. Every BF6_POLL_INTERVAL_SECONDS
    (default 300s / 5 min) it iterates every profile stored in SQLite and re-fetches
    via gametools using the stored platformUserIdentifier (which is the nucleus id —
    stable across name changes). Refresh path reuses upsert_profile_with_delta so
    the delta match is persisted automatically on any counter movement.

The DB is keyed by platformUserIdentifier (gametools userId), not by (name, platform).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import copy
import contextlib
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    # normal case: `uvicorn app.api:app` or `python -m app.api` from the repo root
    from . import __version__
    from .converter import build_trn_profile, build_trn_matches_response, _pick_player_card
    from .logging_utils import log_event
    from .main import GametoolsClient, StatsStorage
except ImportError:
    # fallback for `python app/api.py` — inject the repo root onto sys.path
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from app import __version__
    from app.converter import build_trn_profile, build_trn_matches_response, _pick_player_card
    from app.logging_utils import log_event
    from app.main import GametoolsClient, StatsStorage

DB_PATH = os.environ.get("BF6_DB_PATH", StatsStorage.DEFAULT_DB_PATH)


def _parse_cors_origins(raw: str) -> List[str]:
    value = (raw or "*").strip()
    if value.lower() in ("*", "any", "all"):
        return ["*"]
    origins = [part.strip() for part in value.split(",") if part.strip()]
    return origins or ["*"]


CORS_ORIGINS = _parse_cors_origins(os.environ.get("BF6_CORS_ORIGINS", "*"))
CORS_ALLOW_ANY = CORS_ORIGINS == ["*"]

# --- background poller config ---------------------------------------------
POLL_ENABLED = os.environ.get("BF6_POLL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
POLL_INTERVAL = max(30, int(os.environ.get("BF6_POLL_INTERVAL_SECONDS", "300")))
# Legacy per-player sleep, retained as a between-batch delay only when
# BF6_POLL_BATCH_WORKERS=1 (sequential mode). Defaults to 0 since the batch
# endpoint already takes 100x fewer requests.
POLL_STAGGER = max(0.0, float(os.environ.get("BF6_POLL_STAGGER_SECONDS", "0.0")))
# Number of players per /bf6/multiple/ POST. Upstream cap is 128, but the
# practical Cloudflare/origin timeout limit is lower for full stat payloads.
POLL_BATCH_SIZE = max(1, min(128, int(os.environ.get("BF6_POLL_BATCH_SIZE", "20"))))
# Concurrent stats-batch POSTs. Keep this modest; too much fanout causes
# intermittent upstream 500/504 responses.
POLL_BATCH_WORKERS = max(1, int(os.environ.get("BF6_POLL_BATCH_WORKERS", "4")))
# Concurrent profile fetches for players whose update_hash actually moved.
# Profile is per-player (no batch endpoint) so this is the main bottleneck
# once the batched stats path is in place.
POLL_PROFILE_WORKERS = max(1, int(os.environ.get("BF6_POLL_PROFILE_WORKERS", "4")))

# --- tracked-count cache config -------------------------------------------
TRACKED_COUNTS_ENABLED = os.environ.get("BF6_TRACKED_COUNTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
TRACKED_COUNTS_INTERVAL = max(60, int(os.environ.get("BF6_TRACKED_COUNTS_INTERVAL_SECONDS", "900")))

# --- anonymous suspicion-report config ------------------------------------
REPORTER_COOKIE_NAME = os.environ.get("BF6_REPORTER_COOKIE_NAME", "bf6_reporter_id")
REPORTER_COOKIE_SECRET = os.environ.get("BF6_REPORTER_COOKIE_SECRET", "bf6-tracker-dev-secret")
REPORTER_COOKIE_SECURE = os.environ.get("BF6_REPORTER_COOKIE_SECURE", "true").strip().lower() in ("1", "true", "yes", "on")
REPORTER_COOKIE_SAMESITE = os.environ.get("BF6_REPORTER_COOKIE_SAMESITE", "none").strip().lower()
REPORTER_COOKIE_MAX_AGE = max(86400, int(os.environ.get("BF6_REPORTER_COOKIE_MAX_AGE_SECONDS", "34560000")))
SUSPICION_ALLOWED_TYPES = {
    t.strip().lower()
    for t in os.environ.get(
        "BF6_SUSPICION_TYPES",
        "aimbot,wallhack,recoil,movement,boosting,other",
    ).split(",")
    if t.strip()
}
SUSPICION_MAX_TYPES = max(1, int(os.environ.get("BF6_SUSPICION_MAX_TYPES", "3")))
SUSPICION_REPORTER_HOUR_LIMIT = max(1, int(os.environ.get("BF6_SUSPICION_REPORTER_HOUR_LIMIT", "30")))
SUSPICION_REPORTER_DAY_LIMIT = max(1, int(os.environ.get("BF6_SUSPICION_REPORTER_DAY_LIMIT", "100")))
SUSPICION_IP_HOUR_LIMIT = max(1, int(os.environ.get("BF6_SUSPICION_IP_HOUR_LIMIT", "300")))
SUSPICION_TARGET_MINUTE_LIMIT = max(1, int(os.environ.get("BF6_SUSPICION_TARGET_MINUTE_LIMIT", "60")))

# --- suspicion CORS origin lock -----------------------------------------
# Only POST /players/{id}/suspicion checks this. Other endpoints use the
# global CORSMiddleware as before. Set to empty string to disable the
# check entirely (not recommended in production).
SUSPICION_CORS_ORIGIN = os.environ.get(
    "BF6_SUSPICION_CORS_ORIGIN",
    "https://battlefield.joarchy.com",
).strip()

client = GametoolsClient()
storage = StatsStorage(DB_PATH)

# shared state for /status and internal use
_poll_state: Dict[str, Any] = {
    "enabled":         POLL_ENABLED,
    "intervalSec":     POLL_INTERVAL,
    "staggerSec":      POLL_STAGGER,
    "batchSize":       POLL_BATCH_SIZE,
    "batchWorkers":    POLL_BATCH_WORKERS,
    "profileWorkers":  POLL_PROFILE_WORKERS,
    "lastRunAt":       None,
    "lastRunCount":    0,
    "lastRunMs":       None,
    "lastRunChanged":  0,
    "lastRunUnchanged": 0,
    "lastRunInvalid":  0,
    "lastErrors":      [],
    "runs":            0,
}

_tracked_counts_state: Dict[str, Any] = {
    "enabled":          TRACKED_COUNTS_ENABLED,
    "intervalSec":      TRACKED_COUNTS_INTERVAL,
    "lastCalculatedAt": None,
    "lastCalculationMs": None,
    "playersTracked":   None,
    "matchesTracked":   None,
    "historyId":        None,
    "runs":             0,
    "lastError":        None,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuspicionReportRequest(BaseModel):
    types: Optional[List[str]] = Field(default=None, max_length=SUSPICION_MAX_TYPES)


def _set_request_log_fields(request: Request, **fields: Any) -> None:
    current = getattr(request.state, "log_fields", {})
    current.update({k: v for k, v in fields.items() if v is not None})
    request.state.log_fields = current


def _sign_reporter_id(reporter_id: str) -> str:
    return hmac.new(
        REPORTER_COOKIE_SECRET.encode("utf-8"),
        reporter_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _pack_reporter_cookie(reporter_id: str) -> str:
    return f"{reporter_id}.{_sign_reporter_id(reporter_id)}"


def _unpack_reporter_cookie(cookie_value: Optional[str]) -> Optional[str]:
    if not cookie_value or "." not in cookie_value:
        return None
    reporter_id, sig = cookie_value.rsplit(".", 1)
    if not reporter_id or not sig:
        return None
    expected = _sign_reporter_id(reporter_id)
    if not hmac.compare_digest(sig, expected):
        return None
    return reporter_id


def _hash_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hmac.new(
        REPORTER_COOKIE_SECRET.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _ensure_reporter_key(request: Request, response: Response) -> str:
    reporter_id = _unpack_reporter_cookie(request.cookies.get(REPORTER_COOKIE_NAME))
    if not reporter_id:
        reporter_id = str(uuid.uuid4())
        response.set_cookie(
            REPORTER_COOKIE_NAME,
            _pack_reporter_cookie(reporter_id),
            max_age=REPORTER_COOKIE_MAX_AGE,
            httponly=True,
            secure=REPORTER_COOKIE_SECURE,
            samesite=REPORTER_COOKIE_SAMESITE,
        )
    return _hash_value(reporter_id) or reporter_id


def _client_ip(request: Request) -> Optional[str]:
    # This service is intended to run behind Cloudflare. CF-Connecting-IP is
    # useful only when the origin is not directly reachable by clients.
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return None


def _normalize_suspicion_types(types: Optional[List[str]]) -> List[str]:
    if not types:
        return []
    normalized: List[str] = []
    seen = set()
    for raw in types:
        t = str(raw or "").strip().lower()
        if not t or t in seen:
            continue
        if t not in SUSPICION_ALLOWED_TYPES:
            allowed = ", ".join(sorted(SUSPICION_ALLOWED_TYPES))
            raise HTTPException(status_code=400, detail=f"Invalid suspicion type '{t}'. Allowed: {allowed}")
        normalized.append(t)
        seen.add(t)
    if len(normalized) > SUSPICION_MAX_TYPES:
        raise HTTPException(status_code=400, detail=f"At most {SUSPICION_MAX_TYPES} suspicion types are allowed")
    return normalized


def _suspicion_dates() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    return {
        "now": now,
        "nowIso": now.isoformat(),
        "today": today.isoformat(),
        "last7Start": (today - timedelta(days=6)).isoformat(),
        "minuteCutoffIso": (now - timedelta(minutes=1)).isoformat(),
        "hourCutoffIso": (now - timedelta(hours=1)).isoformat(),
        "dayCutoffIso": (now - timedelta(days=1)).isoformat(),
    }


def _suspicion_payload(identifier: str, reporter_key: str, dates: Dict[str, Any]) -> Dict[str, Any]:
    summary = storage.get_suspicion_summary(
        identifier,
        today=dates["today"],
        last7_start=dates["last7Start"],
    )
    marked_today = storage.has_suspicion_report_today(
        identifier,
        reporter_key=reporter_key,
        report_date=dates["today"],
    )
    return {
        "identifier": str(identifier),
        "summary": summary,
        "viewer": {
            "markedToday": bool(marked_today),
            "reportDate": dates["today"],
        },
    }


def _tracked_counts_display_timestamp(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y%m%d %H%M UTC")
    except Exception:
        return None


def _tracked_counts_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    calculated_at = snapshot.get("calculatedAt")
    return {
        "playersTracked": int(snapshot.get("playersTracked") or 0),
        "matchesTracked": int(snapshot.get("matchesTracked") or 0),
        "calculatedAt": calculated_at,
        "calculatedAtDisplay": _tracked_counts_display_timestamp(calculated_at),
        "calculationMs": int(snapshot.get("calculationMs") or 0),
        "intervalSec": TRACKED_COUNTS_INTERVAL,
        "historyId": snapshot.get("id") or snapshot.get("historyId"),
    }


def _apply_tracked_counts_state(snapshot: Dict[str, Any]) -> None:
    _tracked_counts_state.update({
        "lastCalculatedAt": snapshot.get("calculatedAt"),
        "lastCalculationMs": int(snapshot.get("calculationMs") or 0),
        "playersTracked": int(snapshot.get("playersTracked") or 0),
        "matchesTracked": int(snapshot.get("matchesTracked") or 0),
        "historyId": snapshot.get("id") or snapshot.get("historyId"),
        "runs": int(_tracked_counts_state.get("runs") or 0) + 1,
        "lastError": None,
    })


# --------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------

def _resolve_identifier(name: str, platform: Optional[str]) -> Optional[str]:
    """Find the platformUserIdentifier for (name, platform).
    Checks the stored profiles table first; falls back to a gametools fetch."""
    platform_key = _clean_name(platform)
    cur = storage.conn.cursor()
    cur.execute(
        "SELECT platform_user_identifier FROM profiles WHERE name=? AND platform=? LIMIT 1",
        (name, platform_key),
    )
    row = cur.fetchone()
    if row and row["platform_user_identifier"]:
        return str(row["platform_user_identifier"])
    stats = client.fetch_stats(name, platform)
    if not _stats_payload_looks_valid(stats):
        return None
    iid = stats.get("userId") or stats.get("id")
    return str(iid) if iid else None


def _get_stored_profile_by_name(name: str, platform: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort local profile lookup for name-only requests."""
    platform_key = _clean_name(platform)
    cur = storage.conn.cursor()
    cur.execute(
        "SELECT platform_user_identifier FROM profiles WHERE name=? AND platform=? LIMIT 1",
        (name, platform_key),
    )
    row = cur.fetchone()
    if not row or not row["platform_user_identifier"]:
        return None
    return storage.get_profile(str(row["platform_user_identifier"]))


def _stats_payload_looks_valid(stats: Optional[Dict[str, Any]]) -> bool:
    """Return True only for a GameTools stats payload that is safe to convert.

    GameTools sometimes responds 200 with a profile-shaped empty body such as
    {"other": [], "playerProfiles": []}. That dict is truthy, but it is not a
    stats payload and the converter expects real stats fields like userId/XP.
    """
    if not isinstance(stats, dict):
        return False
    if not (stats.get("userId") or stats.get("id")):
        return False
    xp = stats.get("XP")
    if not isinstance(xp, list) or not xp or not isinstance(xp[0], dict):
        return False
    return True


def _stats_payload_invalid_reason(stats: Optional[Dict[str, Any]]) -> str:
    if stats is None:
        return "fetch_failed"
    if not isinstance(stats, dict):
        return "not_dict"
    if not (stats.get("userId") or stats.get("id")):
        return "missing_user_id"
    xp = stats.get("XP")
    if not isinstance(xp, list) or not xp:
        return "missing_xp"
    if not isinstance(xp[0], dict):
        return "invalid_xp"
    return "unknown"


def _payload_keys(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    return ",".join(sorted(str(k) for k in payload.keys())[:12])


def _clean_name(value: Optional[Any]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _effective_profile_name(
    *,
    stats: Optional[Dict[str, Any]],
    name: Optional[str],
    stored_name: Optional[str],
    identifier: Optional[str],
) -> str:
    """Choose the display name to persist for a profile row (name-based path).

    Used only by the legacy name-based /profile branch (no identifier supplied).
    The id-based path goes through _resolve_canonical_name instead, which adds
    a GameTools cross-check so URL-supplied names cannot poison the store.

    GameTools is queried by `name` here, so `stats.userName` is reflective of
    the lookup key by definition — it can be trusted as authoritative.
    """
    stats_name = _clean_name((stats or {}).get("userName") or (stats or {}).get("username"))
    if stats_name:
        return stats_name

    provided_name = _clean_name(name)
    if provided_name:
        return provided_name

    existing_name = _clean_name(stored_name)
    if existing_name:
        return existing_name

    return _clean_name(identifier)


def _verify_name_owns_identifier(
    name: str,
    platform: Optional[str],
    identifier: str,
) -> bool:
    """Return True iff GameTools resolves (name, platform) to user `identifier`.

    Used to gate the persistence of caller-supplied display names. URL-tampered
    or hand-crafted API requests that include a fake `name` are filtered out
    here. A failed lookup (gametools timeout, /bf6/player flake, empty body)
    returns False so we fall back to the stored canonical row instead of
    overwriting it with an unverifiable name. The cost is one extra
    /bf6/stats?name=... call, charged only on first-seen players or on a
    legitimate in-game rename — never on the steady-state refresh path.
    """
    cleaned_name = _clean_name(name)
    cleaned_id = _clean_name(identifier)
    if not cleaned_name or not cleaned_id:
        return False
    try:
        verify = client.fetch_stats(cleaned_name, platform or None)
    except Exception:
        return False
    if not _stats_payload_looks_valid(verify):
        return False
    returned_id = str(verify.get("userId") or verify.get("id") or "")
    return returned_id == cleaned_id


def _resolve_canonical_name(
    *,
    name: Optional[str],
    platform: Optional[str],
    identifier: str,
    stored_name: Optional[str],
) -> str:
    """Choose the display name to persist for an id-based /profile request.

    Trust ladder (highest-first):
      1. Stored row name when the caller-supplied name matches it case-
         insensitively — common case, no extra GameTools call.
      2. Caller-supplied name verified by GameTools name lookup — covers
         first-seen players and legitimate in-game renames.
      3. Stored row name as-is when verification failed or no name was
         supplied — keeps the canonical name stable when GameTools is down
         or when the request comes from a cold-load (URL has identifier only).
      4. Identifier as last resort — should only happen on first-seen players
         where the verify call also failed.
    """
    provided = _clean_name(name)
    stored = _clean_name(stored_name)

    if provided and stored and provided.lower() == stored.lower():
        return stored
    if provided and _verify_name_owns_identifier(provided, platform, identifier):
        return provided
    if stored:
        return stored
    return _clean_name(identifier)


def _profile_payload_has_player_card(profile: Optional[Dict[str, Any]]) -> bool:
    """Return True when the GameTools profile body contains rank/badge data."""
    pc = _pick_player_card(profile)
    if not pc:
        return False
    return any(pc.get(k) not in (None, "", {}) for k in ("rank", "rankImage", "badges"))


def _overview_segment(trn_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = (trn_profile or {}).get("data") or {}
    for seg in data.get("segments") or []:
        if isinstance(seg, dict) and seg.get("type") == "overview":
            return seg
    return {}


def _preserve_cached_profile_metadata(
    trn_profile: Dict[str, Any],
    cached_profile: Optional[Dict[str, Any]],
) -> bool:
    """Copy rank/badge metadata from the stored TRN profile into a fresh profile.

    Used only when /bf6/profile is missing or partial, while /bf6/stats is good.
    This keeps counters current without poisoning stored profile metadata with
    rank=0/badges=0 from an empty GameTools profile body.
    """
    if not cached_profile:
        return False

    cached_overview = _overview_segment(cached_profile)
    fresh_overview = _overview_segment(trn_profile)
    cached_stats = cached_overview.get("stats") or {}
    fresh_stats = fresh_overview.get("stats") or {}

    changed = False
    if "careerPlayerRank" in cached_stats and "careerPlayerRank" in fresh_stats:
        fresh_stats["careerPlayerRank"] = copy.deepcopy(cached_stats["careerPlayerRank"])
        changed = True

    cached_user = ((cached_profile or {}).get("data") or {}).get("userInfo") or {}
    fresh_user = ((trn_profile or {}).get("data") or {}).get("userInfo") or {}
    if "badges" in cached_user and cached_user.get("badges") is not None:
        fresh_user["badges"] = copy.deepcopy(cached_user.get("badges"))
        changed = True

    return changed


def _refresh_by_identifier(
    identifier: str,
    platform: Optional[str],
    fallback_name: str = "",
    ) -> Dict[str, Any]:
    """Fetch gametools stats+profile by player id, build the TRN profile, upsert.
    Returns the deltaInfo dict from upsert_profile_with_delta (plus a couple of
    status flags if the fetch failed).

    Background refreshes never carry a caller-supplied name, so this path uses
    _resolve_canonical_name with name=None — which collapses to "use whatever
    we already have stored, falling back to the identifier on first-seen".
    """
    platform = _clean_name(platform)
    # `name` is intentionally not forwarded to gametools (see fetch_stats_by_id).
    full = client.fetch_full_by_id(identifier, platform or None)
    stats = full["stats"]
    profile = full["profile"]
    existing = storage.get_profile(identifier)
    if not _stats_payload_looks_valid(stats):
        reason = _stats_payload_invalid_reason(stats)
        return {
            "identifier":   identifier,
            "platform":     platform,
            "fetched":      False,
            "changed":      False,
            "firstSeen":    False,
            "profileSaved": False,
            "matchSaved":   False,
            "error":        "stats_fetch_failed",
            "reason":       reason,
        }

    live_name = _resolve_canonical_name(
        name=None,
        platform=platform,
        identifier=identifier,
        stored_name=fallback_name or (existing or {}).get("name"),
    )
    update_hash = client.generate_update_hash(stats)
    trn = build_trn_profile(
        stats=stats,
        profile=profile,
        name=live_name,
        platform=platform,
        update_hash=update_hash,
    )
    profile_metadata_ok = _profile_payload_has_player_card(profile)
    profile_metadata_preserved = False
    if not profile_metadata_ok and existing:
        profile_metadata_preserved = _preserve_cached_profile_metadata(trn, existing["trnProfile"])
    delta_info = storage.upsert_profile_with_delta(
        trn_profile=trn,
        update_hash=update_hash,
        platform=platform,
        name=live_name,
    )
    delta_info["fetched"] = True
    delta_info["gametoolsProfileOk"] = profile_metadata_ok
    delta_info["profileMetadataPreserved"] = profile_metadata_preserved
    return delta_info


def _process_changed_player(
    identifier: str,
    platform: str,
    fallback_name: str,
    stats: Dict[str, Any],
    new_hash: Optional[str],
) -> Dict[str, Any]:
    """Run the upsert pipeline for a single player whose stats already moved.

    Mirrors _refresh_by_identifier but takes pre-fetched stats (from the
    batched endpoint) so the only per-player network call here is the
    /bf6/profile/ fetch needed for fresh rank/playerCard metadata.
    """
    try:
        profile = client.fetch_profile_by_id(identifier, platform or None)
        existing = storage.get_profile(identifier)
        live_name = _resolve_canonical_name(
            name=None,
            platform=platform,
            identifier=identifier,
            stored_name=fallback_name or (existing or {}).get("name"),
        )
        trn = build_trn_profile(
            stats=stats,
            profile=profile,
            name=live_name,
            platform=platform,
            update_hash=new_hash,
        )
        profile_metadata_ok = _profile_payload_has_player_card(profile)
        profile_metadata_preserved = False
        if not profile_metadata_ok and existing:
            profile_metadata_preserved = _preserve_cached_profile_metadata(trn, existing["trnProfile"])
        delta_info = storage.upsert_profile_with_delta(
            trn_profile=trn,
            update_hash=new_hash,
            platform=platform,
            name=live_name,
        )
        return {
            "identifier":               identifier,
            "name":                     live_name,
            "platform":                 platform,
            "changed":                  bool(delta_info.get("changed")),
            "matchSaved":               bool(delta_info.get("matchSaved")),
            "firstSeen":                bool(delta_info.get("firstSeen")),
            "fetched":                  True,
            "gametoolsProfileOk":       profile_metadata_ok,
            "profileMetadataPreserved": profile_metadata_preserved,
        }
    except Exception as e:
        return {"identifier": identifier, "error": repr(e), "fetched": False}


def _poll_once() -> Dict[str, Any]:
    """Refresh every stored profile once via the batched stats endpoint.

    Strategy:
      1. List all profiles (lightweight rows incl. last-known update_hash).
      2. POST them in chunks of POLL_BATCH_SIZE (max 128) to /bf6/multiple/,
         optionally several batches in parallel via POLL_BATCH_WORKERS.
      3. For each player, compare the freshly-batched stats hash to the
         stored update_hash:
           - hash unchanged  -> skip entirely (no profile fetch, no DB write)
           - hash changed or first-seen -> queue for full upsert
      4. Run upserts (which also fetch /bf6/profile/ for rank metadata)
         concurrently via POLL_PROFILE_WORKERS.

    Used by both the background loop and the manual /refresh-all endpoint.
    """
    started = time.monotonic()
    profiles = storage.list_profiles()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    unchanged_count = 0
    invalid_count = 0

    # Build the batch input. Profiles without an identifier (legacy rows)
    # are skipped silently — they cannot be refreshed by id anyway.
    items: List[Dict[str, Any]] = []
    profile_by_id: Dict[str, Dict[str, Any]] = {}
    for p in profiles:
        iid = _clean_name(p.get("platformUserIdentifier"))
        if not iid:
            continue
        platform = _clean_name(p.get("platform"))
        items.append({"player_id": iid, "platform": platform})
        profile_by_id[iid] = p

    if not items:
        duration_ms = int((time.monotonic() - started) * 1000)
        _poll_state.update({
            "lastRunAt":        _iso_now(),
            "lastRunCount":     0,
            "lastRunMs":        duration_ms,
            "lastRunChanged":   0,
            "lastRunUnchanged": 0,
            "lastRunInvalid":   0,
            "lastErrors":       [],
            "runs":             int(_poll_state.get("runs") or 0) + 1,
        })
        return {
            "refreshed":  0,
            "changed":    0,
            "unchanged":  0,
            "invalid":    0,
            "errors":     [],
            "durationMs": duration_ms,
            "results":    [],
        }

    # Phase 1: batch-fetch fresh stats for every tracked player
    try:
        stats_map = client.fetch_stats_batch_by_ids(
            items,
            chunk_size=POLL_BATCH_SIZE,
            max_workers=POLL_BATCH_WORKERS,
            stagger_seconds=POLL_STAGGER,
        )
    except Exception as e:
        log_event("ERROR", "poller.batch_fetch_failed", error=repr(e))
        stats_map = {}

    # Phase 2: classify each player. Unchanged players short-circuit here
    # without a profile fetch or DB write.
    refresh_targets: List[Dict[str, Any]] = []
    for iid, row in profile_by_id.items():
        platform = _clean_name(row.get("platform"))
        fallback_name = _clean_name(row.get("name"))
        stats = stats_map.get(iid)

        if not _stats_payload_looks_valid(stats):
            invalid_count += 1
            errors.append({
                "identifier": iid,
                "error":      "stats_fetch_failed",
                "reason":     _stats_payload_invalid_reason(stats),
            })
            continue

        new_hash = client.generate_update_hash(stats)
        existing_hash = row.get("updateHash")
        if new_hash and existing_hash and new_hash == existing_hash:
            unchanged_count += 1
            continue

        refresh_targets.append({
            "identifier":     iid,
            "platform":       platform,
            "fallback_name":  fallback_name,
            "stats":          stats,
            "new_hash":       new_hash,
        })

    # Phase 3: concurrent upsert (with profile fetch) for changed/first-seen
    if refresh_targets:
        if POLL_PROFILE_WORKERS > 1 and len(refresh_targets) > 1:
            with ThreadPoolExecutor(max_workers=POLL_PROFILE_WORKERS) as pool:
                futures = [
                    pool.submit(
                        _process_changed_player,
                        t["identifier"], t["platform"], t["fallback_name"],
                        t["stats"], t["new_hash"],
                    )
                    for t in refresh_targets
                ]
                for fut in as_completed(futures):
                    info = fut.result()
                    if info.get("fetched"):
                        results.append(info)
                    else:
                        errors.append({
                            "identifier": info.get("identifier"),
                            "error":      info.get("error") or "process_failed",
                        })
        else:
            for t in refresh_targets:
                info = _process_changed_player(
                    t["identifier"], t["platform"], t["fallback_name"],
                    t["stats"], t["new_hash"],
                )
                if info.get("fetched"):
                    results.append(info)
                else:
                    errors.append({
                        "identifier": info.get("identifier"),
                        "error":      info.get("error") or "process_failed",
                    })

        # Optional inter-batch sleep retained for operators who want to throttle
        # gametools more aggressively. Default 0 since the new path is far less
        # chatty than the old per-player loop.
        if POLL_STAGGER and POLL_BATCH_WORKERS == 1:
            time.sleep(POLL_STAGGER)

    duration_ms = int((time.monotonic() - started) * 1000)
    _poll_state.update({
        "lastRunAt":        _iso_now(),
        "lastRunCount":     len(results) + unchanged_count,
        "lastRunMs":        duration_ms,
        "lastRunChanged":   len(results),
        "lastRunUnchanged": unchanged_count,
        "lastRunInvalid":   invalid_count,
        "lastErrors":       errors,
        "runs":             int(_poll_state.get("runs") or 0) + 1,
    })
    return {
        "refreshed":  len(results) + unchanged_count,
        "changed":    len(results),
        "unchanged":  unchanged_count,
        "invalid":    invalid_count,
        "errors":     errors,
        "durationMs": duration_ms,
        "results":    results,
    }


def _tracked_counts_once() -> Dict[str, Any]:
    """Count tracked players/matches once and persist the result for history."""
    started = time.monotonic()
    calculated_at = _iso_now()
    players_tracked = storage.count_tracked_players()
    matches_tracked = storage.count_tracked_matches()
    duration_ms = int((time.monotonic() - started) * 1000)
    snapshot = storage.save_tracked_count_snapshot(
        calculated_at=calculated_at,
        players_tracked=players_tracked,
        matches_tracked=matches_tracked,
        calculation_ms=duration_ms,
    )
    _apply_tracked_counts_state(snapshot)
    return snapshot


async def _tracked_counts_loop() -> None:
    """Background task: periodically refresh cached public tracking counts."""
    log_event(
        "INFO",
        "tracked_counts.started",
        enabled=TRACKED_COUNTS_ENABLED,
        intervalSec=TRACKED_COUNTS_INTERVAL,
    )
    await asyncio.sleep(1)
    while True:
        try:
            snapshot = await asyncio.to_thread(_tracked_counts_once)
            log_event(
                "INFO",
                "tracked_counts.summary",
                players=snapshot.get("playersTracked"),
                matches=snapshot.get("matchesTracked"),
                durationMs=snapshot.get("calculationMs"),
                calculatedAt=snapshot.get("calculatedAt"),
            )
        except Exception as e:
            _tracked_counts_state["lastError"] = repr(e)
            log_event("ERROR", "tracked_counts.unexpected_error", error=repr(e))
        await asyncio.sleep(TRACKED_COUNTS_INTERVAL)


async def _poll_loop() -> None:
    """Background task: wait the configured interval, then run _poll_once once,
    forever. Swallow errors so the task never dies silently."""
    log_event(
        "INFO",
        "poller.started",
        enabled=POLL_ENABLED,
        intervalSec=POLL_INTERVAL,
        batchSize=POLL_BATCH_SIZE,
        batchWorkers=POLL_BATCH_WORKERS,
        profileWorkers=POLL_PROFILE_WORKERS,
        staggerSec=POLL_STAGGER,
    )
    # small initial delay so uvicorn finishes booting before we fire the first batch
    await asyncio.sleep(5)
    while True:
        try:
            summary = await asyncio.to_thread(_poll_once)
            results = summary.get("results") or []
            errors = summary.get("errors") or []
            log_event(
                "INFO",
                "poller.summary",
                refreshed=summary.get("refreshed"),
                changed=summary.get("changed"),
                unchanged=summary.get("unchanged"),
                invalid=summary.get("invalid"),
                matches=sum(1 for r in results if r.get("matchSaved")),
                metadataPreserved=sum(1 for r in results if r.get("profileMetadataPreserved")),
                failed=len(errors),
                durationMs=summary.get("durationMs"),
            )
            if errors:
                sample = ",".join(
                    f"{e.get('identifier')}:{e.get('reason') or e.get('error')}"
                    for e in errors[:5]
                )
                log_event("WARN", "poller.error_sample", errors=len(errors), sample=sample)
        except Exception as e:
            log_event("ERROR", "poller.unexpected_error", error=repr(e))
        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------
# FastAPI lifespan — start / stop the background poller
# --------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: List[asyncio.Task] = []
    if POLL_ENABLED:
        tasks.append(asyncio.create_task(_poll_loop(), name="bf6-poller"))
    if TRACKED_COUNTS_ENABLED:
        tasks.append(asyncio.create_task(_tracked_counts_loop(), name="bf6-tracked-counts"))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


app = FastAPI(title="BF6 Tracker API", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # Starlette returns `Access-Control-Allow-Origin: *` for simple requests
    # when allow_origins=["*"]. Browsers reject that if credentials are
    # included, and the suspicion endpoints need cookies. Regex wildcard makes
    # Starlette echo the request Origin instead, while still accepting any dev
    # origin when BF6_CORS_ORIGINS is "*", "any", or "all".
    allow_origins=[] if CORS_ALLOW_ANY else CORS_ORIGINS,
    allow_origin_regex=".*" if CORS_ALLOW_ANY else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def suspicion_cors_middleware(request: Request, call_next):
    """Restrict CORS preflight and actual responses for the suspicion POST
    endpoint to SUSPICION_CORS_ORIGIN. All other routes pass through
    unchanged to the global CORSMiddleware."""
    if SUSPICION_CORS_ORIGIN and _is_suspicion_post_path(request.url.path):
        req_origin = (request.headers.get("Origin") or "").strip()
        # Preflight: only allow the trusted origin
        if request.method == "OPTIONS":
            if req_origin == SUSPICION_CORS_ORIGIN:
                return Response(
                    status_code=200,
                    headers={
                        "Access-Control-Allow-Origin": SUSPICION_CORS_ORIGIN,
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Max-Age": "86400",
                    },
                )
            # Wrong origin: no CORS headers → browser blocks it
            return Response(status_code=204)

        # Actual POST: override the global CORS header to pin the origin
        response = await call_next(request)
        if req_origin == SUSPICION_CORS_ORIGIN:
            response.headers["Access-Control-Allow-Origin"] = SUSPICION_CORS_ORIGIN
            response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            response.headers.pop("Access-Control-Allow-Origin", None)
        return response

    return await call_next(request)


def _is_suspicion_post_path(path: str) -> bool:
    """Return True for paths like /players/{id}/suspicion (not /check)."""
    parts = path.strip("/").split("/")
    return (
        len(parts) == 3
        and parts[0] == "players"
        and parts[2] == "suspicion"
    )


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    started = time.monotonic()
    qp = request.query_params
    safe_query_fields = {
        "identifier": qp.get("identifier"),
        "name": qp.get("name") or qp.get("query"),
        "platform": qp.get("platform"),
        "limit": qp.get("limit"),
        "offset": qp.get("offset"),
    }
    try:
        response = await call_next(request)
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            "ERROR",
            "http.error",
            method=request.method,
            path=request.url.path,
            durationMs=duration_ms,
            error=type(e).__name__,
        )
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    extra_fields = getattr(request.state, "log_fields", {})
    if request.url.path == "/ping" and response.status_code < 400 and not extra_fields:
        return response
    degraded = bool(extra_fields.get("degraded"))
    served_from_cache = bool(extra_fields.get("cache"))
    level = "WARN" if response.status_code >= 400 or degraded or served_from_cache else "INFO"
    fields = {**safe_query_fields, **extra_fields}
    log_event(
        level,
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        durationMs=duration_ms,
        **fields,
    )
    return response


# --------------------------------------------------------------------
# health / status
# --------------------------------------------------------------------

@app.get("/ping")
def ping() -> Dict[str, Any]:
    # `version` is sourced from app/__init__.py.__version__ — useful so the
    # frontend can verify which backend (UK primary vs US failover) and which
    # build it just hit, without having to scrape OpenAPI.
    return {"ok": True, "service": "bf6-tracker", "version": __version__, "db": DB_PATH}


@app.get("/status")
def status() -> Dict[str, Any]:
    return {
        "service":  "bf6-tracker",
        "version":  __version__,
        "db":       DB_PATH,
        "profiles": len(storage.list_profiles()),
        "poller":   _poll_state,
        "trackedCounts": _tracked_counts_state,
    }


@app.get("/tracked-counts")
def tracked_counts(request: Request) -> Dict[str, Any]:
    snapshot: Optional[Dict[str, Any]] = None
    if _tracked_counts_state.get("lastCalculatedAt"):
        snapshot = {
            "id": _tracked_counts_state.get("historyId"),
            "calculatedAt": _tracked_counts_state.get("lastCalculatedAt"),
            "playersTracked": _tracked_counts_state.get("playersTracked"),
            "matchesTracked": _tracked_counts_state.get("matchesTracked"),
            "calculationMs": _tracked_counts_state.get("lastCalculationMs"),
        }
    else:
        snapshot = storage.get_latest_tracked_count_snapshot()
        if snapshot:
            _apply_tracked_counts_state(snapshot)

    if not snapshot:
        snapshot = _tracked_counts_once()

    _set_request_log_fields(
        request,
        playersTracked=snapshot.get("playersTracked"),
        matchesTracked=snapshot.get("matchesTracked"),
        calculatedAt=snapshot.get("calculatedAt"),
    )
    return {"data": _tracked_counts_payload(snapshot)}


# --------------------------------------------------------------------
# /search — TRN-style search response (single result by exact name)
# --------------------------------------------------------------------

@app.get("/search")
def search(
    request: Request,
    query: str = Query(..., description="Player name"),
    platform: Optional[str] = Query(None),
) -> Dict[str, Any]:
    platform = _clean_name(platform)
    stats = client.fetch_stats(query, platform)
    profile = client.fetch_profile(query, platform)
    if not _stats_payload_looks_valid(stats):
        _set_request_log_fields(request, degraded=True, reason=_stats_payload_invalid_reason(stats))
        return {"data": []}
    user_id = str(stats.get("userId") or stats.get("id") or "")
    return {
        "data": [
            {
                "platformInfo": {
                    "platformSlug": platform,
                    "platformUserId": None,
                    "platformUserHandle": stats.get("userName") or query,
                    "platformUserIdentifier": user_id,
                    "avatarUrl": stats.get("avatar") or None,
                    "additionalParameters": None,
                },
                "userInfo": {
                    "countryCode": None,
                    # v0.0.4.7: _pick_player_card tolerates the degraded
                    # gametools shape where playerProfiles is empty and the
                    # data lives under other[*].playerProfiles[0].
                    "badges": (
                        _pick_player_card(profile).get("badges")
                        if profile else None
                    ),
                },
            }
        ]
    }


# --------------------------------------------------------------------
# /profile — fetch, build TRN, upsert (saves profile + delta match)
# --------------------------------------------------------------------

@app.get("/profile")
def get_profile(
    request: Request,
    name: Optional[str] = Query(None, description="Player display name; persisted when identifier is also given"),
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    platform: Optional[str] = Query(None),
    raw: bool = Query(False, description="Return gametools raw payload instead of TRN-shaped"),
) -> Dict[str, Any]:
    platform = _clean_name(platform)
    if not identifier and not name:
        raise HTTPException(status_code=400, detail="Need either identifier or name")

    # prefer id-based fetch — stable across gametag / name changes
    if identifier:
        existing = storage.get_profile(identifier)
        # Cold-load path (`/p/<id>` URL only): the URL has no platform, so fall
        # back to the platform recorded on the stored row. The frontend's
        # search dropdown is the only path that ships a fresh platform alongside
        # the identifier, and it does so via React Router state — never via
        # the URL — so a missing platform here means "trust what you stored".
        effective_platform = platform or _clean_name((existing or {}).get("platform"))
        # `name` is NOT forwarded to gametools — it would be echoed back as
        # userName and let URL-tampered names land in our store. The canonical
        # display name is resolved separately by _resolve_canonical_name, which
        # cross-checks the caller-supplied name against gametools by-name.
        full = client.fetch_full_by_id(identifier, effective_platform or None)
        stored_name = existing["name"] if existing else None
        # effective_name = _resolve_canonical_name(
        #     name=name,
        #     platform=effective_platform,
        #     identifier=identifier,
        #     stored_name=stored_name,
        # )
        effective_name = name or stored_name
        # Use the resolved platform downstream so a cold-load update of an
        # existing row doesn't blank its `platform` column.
        platform = effective_platform
    else:
        full = client.fetch_full(name, platform)
        existing = _get_stored_profile_by_name(name, platform) if name else None
        effective_name = _effective_profile_name(
            stats=full["stats"],
            name=name,
            stored_name=(existing or {}).get("name"),
            identifier=None,
        )

    stats = full["stats"]
    profile = full["profile"]
    if not _stats_payload_looks_valid(stats):
        reason = _stats_payload_invalid_reason(stats)
        if isinstance(stats, dict):
            log_event(
                "WARN",
                "gametools.invalid_payload",
                endpoint="stats",
                mode="id" if identifier else "name",
                identifier=identifier or (existing or {}).get("platformUserIdentifier"),
                name=name,
                platform=platform,
                reason=reason,
                keys=_payload_keys(stats),
            )
        if existing:
            delta_info = {
                "identifier":     existing["platformUserIdentifier"],
                "changed":        False,
                "firstSeen":      False,
                "profileSaved":   False,
                "matchSaved":     False,
                "toHash":         existing["updateHash"],
                "fromHash":       existing["updateHash"],
                "matchId":        None,
                "updatedAt":      existing["updatedAt"],
                "fetched":        False,
                "servedFromCache": True,
                "error":          "stats_fetch_failed",
                "reason":         reason,
            }
            _set_request_log_fields(
                request,
                identifier=existing["platformUserIdentifier"],
                platform=platform,
                cache=True,
                degraded=True,
                reason="stats_fetch_failed",
                cacheUpdatedAt=existing["updatedAt"],
            )
            log_event(
                "WARN",
                "profile.cache_hit",
                identifier=existing["platformUserIdentifier"],
                platform=platform,
                updatedAt=existing["updatedAt"],
                reason="stats_fetch_failed",
            )
            if raw:
                return {
                    "gametoolsStats":   stats,
                    "gametoolsProfile": profile,
                    "updateHash":       existing["updateHash"],
                    "deltaInfo":        delta_info,
                }
            cached_trn = copy.deepcopy(existing["trnProfile"])
            cached_trn["deltaInfo"] = delta_info
            return cached_trn
        _set_request_log_fields(request, platform=platform, degraded=True, reason="stats_fetch_failed")
        raise HTTPException(status_code=502, detail="Failed to fetch gametools stats")

    stats_identifier = str(stats.get("userId") or stats.get("id") or "")
    if not existing and stats_identifier:
        existing = storage.get_profile(stats_identifier)
    if not identifier:
        effective_name = _effective_profile_name(
            stats=stats,
            name=name,
            stored_name=(existing or {}).get("name"),
            identifier=stats_identifier,
        )

    update_hash = client.generate_update_hash(stats)

    trn = build_trn_profile(
        stats=stats,
        profile=profile,
        name=effective_name,
        platform=platform,
        update_hash=update_hash,
    )
    profile_metadata_ok = _profile_payload_has_player_card(profile)
    profile_metadata_preserved = False
    if not profile_metadata_ok and existing:
        profile_metadata_preserved = _preserve_cached_profile_metadata(trn, existing["trnProfile"])
        if profile_metadata_preserved:
            log_event(
                "WARN",
                "profile.metadata_preserved",
                identifier=stats_identifier,
                platform=platform,
                updatedAt=existing["updatedAt"],
                reason="profile_missing_player_card",
            )
    if not profile_metadata_ok and isinstance(profile, dict):
        log_event(
            "WARN",
            "gametools.invalid_payload",
            endpoint="profile",
            mode="id" if identifier else "name",
            identifier=stats_identifier or identifier,
            name=name,
            platform=platform,
            reason="empty_player_card",
            keys=_payload_keys(profile),
        )

    delta_info = storage.upsert_profile_with_delta(
        trn_profile=trn,
        update_hash=update_hash,
        platform=platform,
        name=effective_name,
    )
    delta_info["fetched"] = True
    delta_info["servedFromCache"] = False
    delta_info["gametoolsProfileOk"] = profile_metadata_ok
    delta_info["profileMetadataPreserved"] = profile_metadata_preserved
    _set_request_log_fields(
        request,
        identifier=delta_info.get("identifier") or stats_identifier or identifier,
        platform=platform,
        cache=False,
        degraded=not profile_metadata_ok,
        reason=None if profile_metadata_ok else "profile_missing_player_card",
        changed=bool(delta_info.get("changed")),
        matchSaved=bool(delta_info.get("matchSaved")),
        metadataPreserved=profile_metadata_preserved,
    )

    if raw:
        return {
            "gametoolsStats":   stats,
            "gametoolsProfile": profile,
            "updateHash":       update_hash,
            "deltaInfo":        delta_info,
        }

    trn["deltaInfo"] = delta_info
    return trn


# --------------------------------------------------------------------
# /matches — TRN-shaped matches response keyed by platformUserIdentifier
# --------------------------------------------------------------------

@app.get("/matches")
def get_matches(
    request: Request,
    name: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    platform = _clean_name(platform)
    iid = identifier or (_resolve_identifier(name, platform) if name else None)
    if not iid:
        _set_request_log_fields(request, platform=platform, reason="identifier_resolution_failed")
        raise HTTPException(status_code=400, detail="Need either identifier or name")
    _set_request_log_fields(request, identifier=iid, platform=platform, limit=limit, offset=offset)

    matches = storage.list_profile_matches(iid, limit=limit, offset=offset)
    total = storage.count_profile_matches(iid)
    next_page = (offset // limit + 2) if (offset + limit) < total else None

    return build_trn_matches_response(
        matches,
        account_id=iid,
        next_page=next_page,
    )


# --------------------------------------------------------------------
# /players/{identifier}/suspicion — anonymous one-way suspicion marks
# --------------------------------------------------------------------

@app.get("/players/{identifier}/suspicion")
def get_player_suspicion(
    identifier: str,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    iid = str(identifier or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="Need player identifier")
    reporter_key = _ensure_reporter_key(request, response)
    dates = _suspicion_dates()
    _set_request_log_fields(request, identifier=iid)
    return {"data": _suspicion_payload(iid, reporter_key, dates)}


@app.get("/players/{identifier}/suspicion/check")
def check_player_suspicion_mark(
    identifier: str,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    iid = str(identifier or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="Need player identifier")
    reporter_key = _ensure_reporter_key(request, response)
    dates = _suspicion_dates()
    marked_today = storage.has_suspicion_report_today(
        iid,
        reporter_key=reporter_key,
        report_date=dates["today"],
    )
    _set_request_log_fields(request, identifier=iid)
    return {
        "data": {
            "identifier": iid,
            "markedToday": bool(marked_today),
            "reportDate": dates["today"],
        }
    }


@app.post("/players/{identifier}/suspicion")
def mark_player_suspicion(
    identifier: str,
    payload: SuspicionReportRequest,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    # --- suspicion-specific CORS origin lock ---
    if SUSPICION_CORS_ORIGIN:
        req_origin = (request.headers.get("Origin") or "").strip()
        if req_origin != SUSPICION_CORS_ORIGIN:
            _set_request_log_fields(
                request,
                identifier=str(identifier or "").strip(),
                degraded=True,
                reason=f"suspicion_cors_rejected:{req_origin[:80]}",
            )
            raise HTTPException(
                status_code=403,
                detail="Origin not allowed for suspicion reports",
            )

    iid = str(identifier or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="Need player identifier")

    types = _normalize_suspicion_types(payload.types)
    dates = _suspicion_dates()
    reporter_key = _ensure_reporter_key(request, response)
    reporter_ip_hash = _hash_value(_client_ip(request))
    user_agent_hash = _hash_value(request.headers.get("User-Agent"))
    cf_ray = request.headers.get("CF-Ray")

    limited = storage.check_and_record_suspicion_attempt(
        reporter_key=reporter_key,
        reporter_ip_hash=reporter_ip_hash,
        target_platform_user_identifier=iid,
        now_iso=dates["nowIso"],
        minute_cutoff_iso=dates["minuteCutoffIso"],
        hour_cutoff_iso=dates["hourCutoffIso"],
        day_cutoff_iso=dates["dayCutoffIso"],
        reporter_hour_limit=SUSPICION_REPORTER_HOUR_LIMIT,
        reporter_day_limit=SUSPICION_REPORTER_DAY_LIMIT,
        ip_hour_limit=SUSPICION_IP_HOUR_LIMIT,
        target_minute_limit=SUSPICION_TARGET_MINUTE_LIMIT,
    )
    if limited:
        retry_after = str(int(limited.get("retryAfterSec") or 60))
        _set_request_log_fields(
            request,
            identifier=iid,
            degraded=True,
            reason=f"suspicion_rate_limited:{limited.get('scope')}",
        )
        raise HTTPException(
            status_code=429,
            detail="Too many suspicion reports. Try again later.",
            headers={"Retry-After": retry_after},
        )

    report = storage.create_suspicion_report(
        target_platform_user_identifier=iid,
        reporter_key=reporter_key,
        report_date=dates["today"],
        types=types,
        created_at=dates["nowIso"],
        reporter_ip_hash=reporter_ip_hash,
        cf_ray=cf_ray,
        user_agent_hash=user_agent_hash,
    )
    data = _suspicion_payload(iid, reporter_key, dates)
    data.update({
        "markedToday": True,
        "alreadyMarkedToday": bool(report["alreadyMarkedToday"]),
        "reportDate": dates["today"],
    })
    _set_request_log_fields(
        request,
        identifier=iid,
        suspicionCreated=bool(report["created"]),
        suspicionTypes=",".join(types) if types else "none",
    )
    return {"data": data}


# --------------------------------------------------------------------
# /refresh — manually refresh one player (by identifier or by name)
# --------------------------------------------------------------------

@app.post("/refresh")
def refresh(
    request: Request,
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    name: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
) -> Dict[str, Any]:
    platform = _clean_name(platform)
    iid = identifier or (_resolve_identifier(name, platform) if name else None)
    if not iid:
        _set_request_log_fields(request, platform=platform, reason="identifier_resolution_failed")
        raise HTTPException(status_code=400, detail="Need either identifier or name")

    existing = storage.get_profile(iid)
    fallback_name = name or (existing["name"] if existing else "")
    info = _refresh_by_identifier(iid, platform, fallback_name)
    if not info.get("fetched", True):
        _set_request_log_fields(
            request,
            identifier=iid,
            platform=platform,
            degraded=True,
            reason=info.get("reason") or info.get("error"),
        )
        raise HTTPException(status_code=502, detail=f"Gametools fetch failed for {iid}")
    _set_request_log_fields(
        request,
        identifier=iid,
        platform=platform,
        degraded=not bool(info.get("gametoolsProfileOk", True)),
        reason=None if info.get("gametoolsProfileOk", True) else "profile_missing_player_card",
        changed=bool(info.get("changed")),
        matchSaved=bool(info.get("matchSaved")),
        metadataPreserved=bool(info.get("profileMetadataPreserved")),
    )
    return {"deltaInfo": info}


# --------------------------------------------------------------------
# /refresh-all — fire the background poll immediately (manual trigger)
# --------------------------------------------------------------------

@app.post("/refresh-all")
async def refresh_all() -> Dict[str, Any]:
    summary = await asyncio.to_thread(_poll_once)
    return summary


# --------------------------------------------------------------------
# /profiles — lightweight listing of everything currently being tracked
# --------------------------------------------------------------------

@app.get("/profiles")
def list_profiles() -> Dict[str, Any]:
    return {"data": storage.list_profiles()}


if __name__ == "__main__":
    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=False, access_log=False)


# =====================================================================
# Build / release
#
# The repo-root `build.sh` script automates building the linux/amd64
# image, tagging it with the current `__version__`, and exporting a
# `bf6-tracker-amd64-v<version>.tar` tarball ready to be copied to the
# NAS. It reads the version from `app/__init__.py` so there is nothing
# to keep in sync.
#
#   ./build.sh                  # build + tag + save tarball
#   ./build.sh --push <registry>  # (optional) push instead of save
#
# Deploy on the NAS:
#
#   scp bf6-tracker-amd64-v<version>.tar user@nas:/volume1/docker/bf6-tracker/
#   ssh user@nas "cd /volume1/docker/bf6-tracker \
#                  && docker load -i bf6-tracker-amd64-v<version>.tar \
#                  && docker compose up -d"
#
# Inspect the running version:
#
#   curl -s http://<nas>:8000/ping | jq .version
# =====================================================================
