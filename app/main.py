import requests
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Union

# v0.0.5: gzip-compress the two big JSON blob columns (match_json,
# trn_profile_json) at write time and decompress transparently at read.
# See app/storage_codec.py for the on-disk encoding (magic-byte prefix lets
# legacy v0.0.4 raw-JSON rows still read correctly until the migration pass
# rewrites them).
try:
    from . import storage_codec as _codec
    from . import image_dict as _image_dict
    from .logging_utils import log_event
except ImportError:
    from app import storage_codec as _codec  # type: ignore
    from app import image_dict as _image_dict  # type: ignore
    from app.logging_utils import log_event  # type: ignore


# ============================================================
# 0) time helpers
# ============================================================

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_param(value: Optional[Any]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_zero_delta_match(match_obj: Dict[str, Any]) -> bool:
    """Return True if `match_obj` is a "junk" delta — every overview counter is
    zero AND every per-group metadata bucket (gamemodes/kits/levels/weapons/
    vehicles/gadgets) is empty. These rows show up when the gametools update
    hash flips without any real gameplay (e.g. per-class secondsPlayed jitter
    in `_apply_corrections`); the resulting match is meaningless on the
    /matches feed because it advertises no movement.

    `careerPlayerRank.value` is intentionally excluded because it carries the
    player's *current* rank (not a counter) — its delta lives in
    `careerPlayerRank.metadata.delta`. We do NOT treat a non-zero rank delta
    as movement on its own: rank can wobble due to leaderboard recalcs even
    when no counters changed, and a "match" showing only "rank -1" with all
    other stats at zero is exactly the noise the user wants gone.
    """
    segs = (match_obj or {}).get("segments") or []
    if not segs:
        return False
    overview = next((s for s in segs if isinstance(s, dict) and s.get("type") == "overview"), None)
    if not overview:
        return False

    stats = overview.get("stats") or {}
    for key, blk in stats.items():
        if key == "careerPlayerRank":
            continue
        if not isinstance(blk, dict):
            continue
        try:
            v = float(blk.get("value") or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v != 0:
            return False

    md = overview.get("metadata") or {}
    for groupname in ("gamemodes", "kits", "levels", "weapons", "vehicles", "gadgets"):
        if md.get(groupname):
            return False

    return True


# ============================================================
# 1) Gametools client (with your correction logic)
# ============================================================

class GametoolsClient:
    def __init__(self):
        self.base_url_profile = "https://api.gametools.network/bf6/profile/"
        self.base_url_stats = "https://api.gametools.network/bf6/stats/"
        # Batched stats endpoint — accepts up to 128 players per POST request.
        # Returns the same per-player stats payload shape as /bf6/stats/, wrapped
        # in {"data": [item, item, ...]}. Used by the background poller via
        # fetch_stats_batch_by_ids() to avoid per-player request fanout.
        self.base_url_stats_multi = "https://api.gametools.network/bf6/multiple/"
        self.params = {
            "raw": "false",
            "format_values": "true",
            "skip_battlelog": "true"
        }
        # Keep-alive Session avoids a TCP/TLS handshake per request — a big
        # win on the poller path which otherwise opens 2 connections per
        # player. Session.get/Session.post are documented thread-safe for
        # independent requests, so the poller can call this from a
        # ThreadPoolExecutor without locking.
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "bf6-tracker-backend/0.0.7.8",
        })

    def _apply_corrections(self, data_stats: Dict[str, Any]) -> Dict[str, Any]:
        """Correct the buggy aggregate secondsPlayed reported by gametools by
        summing per-class secondsPlayed."""
        try:
            if isinstance(data_stats, dict) and "classes" in data_stats and isinstance(data_stats["classes"], list):
                corrected_seconds = sum(int(c.get("secondsPlayed", 0) or 0) for c in data_stats["classes"] if isinstance(c, dict) and c.get("className") != "All")
                data_stats["_rawSecondsPlayed"] = data_stats.get("secondsPlayed", 0)
                data_stats["secondsPlayed"] = corrected_seconds

                hours = corrected_seconds // 3600
                rem = corrected_seconds % 3600
                minutes = rem // 60
                data_stats["timePlayed"] = f"{int(hours)} H, {minutes} M" if hours > 0 else f"{minutes} minutes"

                total_kills = int(data_stats.get("kills", 0) or 0)
                if corrected_seconds > 0:
                    data_stats["killsPerMinute"] = round(total_kills / (corrected_seconds / 60), 2)
                else:
                    data_stats["killsPerMinute"] = 0.0
            # -------------------------------------------

            return data_stats
        except Exception as e:
            log_event("WARN", "gametools.correction_failed", error=type(e).__name__)
            return data_stats

    def fetch_stats(self, name: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/stats/ by player name (corrected)."""
        params = {**self.params, "name": name}
        if _clean_param(platform):
            params["platform"] = _clean_param(platform)
        try:
            r = self._session.get(self.base_url_stats, params=params, timeout=10)
            r.raise_for_status()
            return self._apply_corrections(r.json())
        except Exception:
            return None

    def fetch_profile(self, name: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/profile/ by player name (rank / playerCard metadata)."""
        params = {**self.params, "name": name}
        if _clean_param(platform):
            params["platform"] = _clean_param(platform)
        try:
            r = self._session.get(self.base_url_profile, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def fetch_stats_by_id(
        self,
        player_id: Union[str, int],
        platform: Optional[str] = None,
        name: Optional[str] = None,  # accepted for caller compatibility — see note
    ) -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/stats/ keyed by nucleus/player id. Preferred for the
        background poller because it is stable across name changes.

        IMPORTANT: `name` is accepted for caller compatibility but intentionally
        NOT forwarded to gametools. The upstream service echoes the `name`
        query back as `userName` in the response, which let URL-tampered names
        slip into the canonical store. The api layer resolves the display
        name out-of-band via _resolve_canonical_name (see api.py).
        """
        params = {
            **self.params,
            "playerid":  str(player_id),
            "nucleus_id": str(player_id),
        }
        if _clean_param(platform):
            params["platform"] = _clean_param(platform)
        # Deliberately not forwarding `name` to gametools. See docstring.
        try:
            r = self._session.get(self.base_url_stats, params=params, timeout=10)
            r.raise_for_status()
            return self._apply_corrections(r.json())
        except Exception:
            return None

    def fetch_profile_by_id(
        self,
        player_id: Union[str, int],
        platform: Optional[str] = None,
        name: Optional[str] = None,  # accepted for caller compatibility — see fetch_stats_by_id
    ) -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/profile/ keyed by nucleus/player id.

        `name` is accepted but not forwarded — see fetch_stats_by_id for why.
        """
        params = {
            **self.params,
            "playerid":  str(player_id),
            "nucleus_id": str(player_id),
        }
        if _clean_param(platform):
            params["platform"] = _clean_param(platform)
        # Deliberately not forwarding `name` to gametools. See fetch_stats_by_id.
        try:
            r = self._session.get(self.base_url_profile, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def fetch_full(self, name: str, platform: Optional[str] = None) -> Dict[str, Any]:
        """Fetch both stats + profile in one call (by name); either may be None on error."""
        return {
            "stats": self.fetch_stats(name, platform),
            "profile": self.fetch_profile(name, platform),
        }

    def fetch_full_by_id(
        self,
        player_id: Union[str, int],
        platform: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch both stats + profile in one call (by id); either may be None on error."""
        return {
            "stats": self.fetch_stats_by_id(player_id, platform, name=name),
            "profile": self.fetch_profile_by_id(player_id, platform, name=name),
        }

    # --- batched stats fetch -------------------------------------------------
    # /bf6/multiple/ accepts a JSON array of {player_id, user_id, platform}
    # and returns {"data": [stats, ...]}. The upstream cap is 128 entries
    # per request. The poller uses this to refresh 1000+ players in a handful
    # of round-trips instead of 2 round-trips per player.

    BATCH_STATS_MAX = 128

    def fetch_stats_batch_by_ids(
        self,
        items: List[Dict[str, Any]],
        chunk_size: int = BATCH_STATS_MAX,
        max_workers: int = 1,
        timeout: int = 30,
        stagger_seconds: float = 0.0,
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """Batch-fetch corrected stats for many players via /bf6/multiple/.

        ``items`` must be an iterable of dicts with at least ``player_id`` and
        ``platform`` keys (``user_id`` is auto-filled from ``player_id`` when
        absent — the upstream API expects both fields and uses identical
        values for them).

        Returns a mapping of ``str(player_id) -> stats_payload`` for every id
        that appeared in the input. Players whose lookup failed (network
        error, upstream omission) map to ``None`` so callers can distinguish
        "not refreshed" from "refreshed but no movement". The mapping
        preserves the corrections that ``fetch_stats_by_id`` would apply.

        ``chunk_size`` is bounded to ``BATCH_STATS_MAX`` (128). ``max_workers``
        controls how many chunks are POSTed concurrently; values >1 use a
        ThreadPoolExecutor over ``self._session`` (Session is thread-safe for
        independent requests). ``timeout`` is per-chunk. ``stagger_seconds``
        sleeps between sequential requests and rescue requests.
        """
        if not items:
            return {}

        chunk_size = max(1, min(int(chunk_size or 1), self.BATCH_STATS_MAX))
        max_workers = max(1, int(max_workers or 1))
        stagger_seconds = max(0.0, float(stagger_seconds or 0.0))
        log_success = os.environ.get("BF6_POLL_LOG_BATCH_SUCCESS", "false").strip().lower() in ("1", "true", "yes", "on")
        retry_statuses = {429, 500, 502, 503, 504}
        retry_delays = [1.0, 3.0]

        # Normalize: dedupe by player_id, drop empties, build {pid: platform}
        seen: Dict[str, Dict[str, str]] = {}
        for it in items:
            pid = _clean_param((it or {}).get("player_id") or (it or {}).get("user_id"))
            if not pid:
                continue
            platform = _clean_param((it or {}).get("platform"))
            seen.setdefault(pid, {"player_id": pid, "user_id": pid, "platform": platform})

        ordered = list(seen.values())
        if not ordered:
            return {}

        # Default-fail every requested id so callers can detect missing entries.
        out: Dict[str, Optional[Dict[str, Any]]] = {pid: None for pid in seen.keys()}

        # Match the curl sample's query string exactly — categories=multiplayer
        # is what makes the response shape mirror /bf6/stats/.
        params = {
            "categories":     "multiplayer",
            "raw":            "false",
            "format_values":  "true",
            "seperation":     "false",
            "lang":           "en-us",
        }

        def _chunk_sample_ids(chunk: List[Dict[str, str]], limit: int = 5) -> str:
            return ",".join(str(item.get("player_id") or "") for item in chunk[:limit])

        def _response_snippet(response: Optional[requests.Response], limit: int = 500) -> Optional[str]:
            if response is None:
                return None
            text = (response.text or "").replace("\n", " ").replace("\r", " ").strip()
            return text[:limit] if text else None

        def _post_chunk(
            chunk: List[Dict[str, str]],
            *,
            rescue: bool = False,
        ) -> Tuple[List[Tuple[str, Optional[Dict[str, Any]]]], bool]:
            payload = [
                {
                    "player_id": int(item["player_id"]) if str(item["player_id"]).isdigit() else item["player_id"],
                    "user_id":   int(item["user_id"])   if str(item["user_id"]).isdigit()   else item["user_id"],
                    "platform":  item.get("platform") or "",
                }
                for item in chunk
            ]

            body: Any = None
            attempts = len(retry_delays) + 1
            for attempt in range(attempts):
                try:
                    r = self._session.post(
                        self.base_url_stats_multi,
                        params=params,
                        json=payload,
                        timeout=timeout,
                    )
                    r.raise_for_status()
                    body = r.json()
                    break
                except requests.HTTPError as e:
                    response = e.response
                    status_code = getattr(response, "status_code", None)
                    if status_code in retry_statuses and attempt < len(retry_delays):
                        delay = retry_delays[attempt]
                        log_event(
                            "WARN",
                            "gametools.batch_stats_chunk_retry",
                            chunkSize=len(chunk),
                            sampleIds=_chunk_sample_ids(chunk),
                            attempt=attempt + 1,
                            retryInSec=delay,
                            rescue=rescue,
                            error=type(e).__name__,
                            statusCode=status_code,
                            reason=getattr(response, "reason", None),
                        )
                        time.sleep(delay)
                        continue
                    log_event(
                        "WARN",
                        "gametools.batch_stats_chunk_failed",
                        chunkSize=len(chunk),
                        sampleIds=_chunk_sample_ids(chunk),
                        attempts=attempt + 1,
                        rescue=rescue,
                        error=type(e).__name__,
                        statusCode=status_code,
                        reason=getattr(response, "reason", None),
                        response=_response_snippet(response),
                    )
                    return [], False
                except (requests.Timeout, requests.ConnectionError) as e:
                    if attempt < len(retry_delays):
                        delay = retry_delays[attempt]
                        log_event(
                            "WARN",
                            "gametools.batch_stats_chunk_retry",
                            chunkSize=len(chunk),
                            sampleIds=_chunk_sample_ids(chunk),
                            attempt=attempt + 1,
                            retryInSec=delay,
                            rescue=rescue,
                            error=type(e).__name__,
                        )
                        time.sleep(delay)
                        continue
                    log_event(
                        "WARN",
                        "gametools.batch_stats_chunk_failed",
                        chunkSize=len(chunk),
                        sampleIds=_chunk_sample_ids(chunk),
                        attempts=attempt + 1,
                        rescue=rescue,
                        error=type(e).__name__,
                    )
                    return [], False
                except Exception as e:
                    log_event(
                        "WARN",
                        "gametools.batch_stats_chunk_failed",
                        chunkSize=len(chunk),
                        sampleIds=_chunk_sample_ids(chunk),
                        attempts=attempt + 1,
                        rescue=rescue,
                        error=type(e).__name__,
                    )
                    return [], False

            data = body.get("data") if isinstance(body, dict) else body
            if not isinstance(data, list) and isinstance(body, dict) and (body.get("userId") or body.get("id")):
                # GameTools returns a bare stats object instead of {"data": [...]}
                # when /bf6/multiple/ is called with exactly one player.
                data = [body]
            if not isinstance(data, list):
                log_event(
                    "WARN",
                    "gametools.batch_stats_unexpected_shape",
                    chunkSize=len(chunk),
                    sampleIds=_chunk_sample_ids(chunk),
                    bodyType=type(body).__name__,
                    keys=",".join(sorted(str(k) for k in body.keys())[:12]) if isinstance(body, dict) else None,
                )
                return [], False

            if log_success or rescue:
                log_event(
                    "INFO",
                    "gametools.batch_stats_chunk_succeeded",
                    chunkSize=len(chunk),
                    resultCount=len(data),
                    sampleIds=_chunk_sample_ids(chunk),
                    rescue=rescue,
                )

            results: List[Tuple[str, Optional[Dict[str, Any]]]] = []
            for stats in data:
                if not isinstance(stats, dict):
                    continue
                # Identify which input pid this row corresponds to. The
                # upstream echoes `id`/`userId` and (some platforms aside)
                # preserves request order; we trust the explicit id over
                # positional matching to be safe against missing/dropped rows.
                pid = _clean_param(stats.get("userId") or stats.get("id"))
                if not pid:
                    continue
                results.append((pid, self._apply_corrections(stats)))
            return results, True

        def _apply_results(chunk_results: List[Tuple[str, Optional[Dict[str, Any]]]]) -> int:
            applied = 0
            for pid, stats in chunk_results:
                if pid in out:
                    out[pid] = stats
                    applied += 1
            return applied

        chunks = [ordered[i:i + chunk_size] for i in range(0, len(ordered), chunk_size)]
        failed_chunks: List[List[Dict[str, str]]] = []

        if max_workers > 1 and len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for chunk, (chunk_results, ok) in zip(chunks, pool.map(_post_chunk, chunks)):
                    if ok:
                        _apply_results(chunk_results)
                    else:
                        failed_chunks.append(chunk)
        else:
            for idx, chunk in enumerate(chunks):
                chunk_results, ok = _post_chunk(chunk)
                if ok:
                    _apply_results(chunk_results)
                else:
                    failed_chunks.append(chunk)
                if stagger_seconds and idx < len(chunks) - 1:
                    time.sleep(stagger_seconds)

        if failed_chunks:
            log_event(
                "WARN",
                "gametools.batch_stats_rescue_started",
                chunks=len(failed_chunks),
                players=sum(len(chunk) for chunk in failed_chunks),
            )
            recovered = 0
            still_failed = 0
            for idx, chunk in enumerate(failed_chunks):
                chunk_results, ok = _post_chunk(chunk, rescue=True)
                if ok:
                    recovered += _apply_results(chunk_results)
                else:
                    still_failed += len(chunk)
                if stagger_seconds and idx < len(failed_chunks) - 1:
                    time.sleep(stagger_seconds)
            log_event(
                "INFO",
                "gametools.batch_stats_rescue_summary",
                chunks=len(failed_chunks),
                recovered=recovered,
                stillFailed=still_failed,
            )

        return out

    def fetch_stats_mock_old(self, name: str, platform: str = "") -> Optional[Dict[str, Any]]:
        data = json.load(open("old.json", "r", encoding="utf-8"))
        return data
    
    def fetch_stats_mock_new(self, name: str, platform: str = "") -> Optional[Dict[str, Any]]:
        data = json.load(open("new.json", "r", encoding="utf-8"))
        return data
    
    # --- canonical fields used to decide "did the player play more?" ---------
    HASH_COUNTER_FIELDS = [
        "kills", "deaths", "wins", "loses", "matchesPlayed", "secondsPlayed",
        "shotsFired", "shotsHit", "killAssists", "revives", "heals",
        "resupplies", "repairs", "vehiclesDestroyed", "enemiesSpotted",
        "headShots", "damage", "saviorKills", "thrownThrowables",
        "gadgetsDestoyed", "playerTakeDowns", "squadmateRevive",
    ]

    def generate_update_hash(self, data: Dict[str, Any]) -> Optional[str]:
        """Canonical sha1 of every counter we care about — any real movement
        invalidates the hash so upsert_with_delta will save a new snapshot +
        produce a delta match. Truncated to 8 chars for readability."""
        if not data:
            return None
        parts = [f"{k}={int(data.get(k, 0) or 0)}" for k in self.HASH_COUNTER_FIELDS]
        # nested bits that also change independently
        obj = data.get("objective") or {}
        t = obj.get("time") or {}
        parts += [
            f"obj.time.total={int(t.get('total', 0) or 0)}",
            f"obj.captured={int(obj.get('captured', 0) or 0)}",
            f"obj.defused={int(obj.get('defused', 0) or 0)}",
            f"sector.captured={int((data.get('sector') or {}).get('captured', 0) or 0)}",
        ]
        dk = data.get("dividedKills") or {}
        for k in ("human", "ads", "hipfire", "melee", "multiKills", "vehicle", "roadkills"):
            parts.append(f"dk.{k}={int(dk.get(k, 0) or 0)}")
        blob = "|".join(parts)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


# ============================================================
# 2) Delta + TRN-like matches builder
#    - key uses gametools item["id"]
#    - imageUrl uses item["image"] or item["altImage"]
# ============================================================

def _int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def _float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def _sub_nonneg(new_v, old_v) -> int:
    d = _int(new_v) - _int(old_v)
    return d if d > 0 else 0

def safe_ratio(n, d, ndigits=3) -> float:
    n = _float(n, 0.0)
    d = _float(d, 0.0)
    if d <= 0:
        return round(n, ndigits)
    return round(n / d, ndigits)

def safe_kpm(kills, seconds_played, ndigits=2) -> float:
    k = _float(kills, 0.0)
    sp = _float(seconds_played, 0.0)
    if sp <= 0:
        return 0.0
    return round(k / (sp / 60.0), ndigits)

def trn_stat(display_name: str, display_category: str, category: str, value, display_type: str = "Number") -> Dict[str, Any]:
    return {
        "displayName": display_name,
        "displayCategory": display_category,
        "category": category,
        "metadata": {},
        "value": value,
        "displayValue": str(value),
        "displayType": display_type,
    }

def _list_to_map_by_id(arr: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    m: Dict[str, Dict[str, Any]] = {}
    for it in arr or []:
        if isinstance(it, dict) and "id" in it:
            m[str(it["id"])] = it
    return m

def _diff_item(old_it: Dict[str, Any], new_it: Dict[str, Any], field_map: Dict[str, Union[str, Tuple[str, str]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for out_f, fields in field_map.items():
        if isinstance(fields, (tuple, list)):
            new_f = fields[0]
            old_f = fields[1] if len(fields) > 1 else fields[0]
        else:
            new_f = old_f = fields
        out[out_f] = _sub_nonneg(new_it.get(new_f, 0), old_it.get(old_f, 0))
    return out

def _item_name(it: Dict[str, Any], name_fields: List[str]) -> str:
    for f in name_fields:
        v = it.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(it.get("id", ""))

def _item_image(it: Dict[str, Any]) -> str:
    # v0.0.6: route through image_dict.resolve() so operator overrides
    # (data/image_dict.json) can fill in for gametools' flaky CDN.
    if not isinstance(it, dict):
        return ""
    gt = it.get("image") or it.get("altImage") or ""
    return _image_dict.resolve(it.get("id"), gt)


# ---- overview delta ----
TOP_COUNTER_FIELDS = [
    "kills", "deaths", "wins", "loses", "matchesPlayed", "secondsPlayed",
    "shotsFired", "shotsHit",
    "killAssists",
    "revives", "heals", "resupplies", "repairs",
    "vehiclesDestroyed", "enemiesSpotted",
    "damage",
    "headshots", "headshotKills",
    "distanceTraveled",
    "objective", "sector", "XP",
    "squadmateRevive",
    "thrownThrowables",
    "gadgetsDestoyed",
    "playerTakeDowns",
    "saviorKills",
]

def compute_overview_delta(old_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, Any]:
    old_data = old_data or {}
    new_data = new_data or {}

    d = {f: _sub_nonneg(new_data.get(f, 0), old_data.get(f, 0)) for f in TOP_COUNTER_FIELDS}
    d["killsPerMinute"] = safe_kpm(d.get("kills", 0), d.get("secondsPlayed", 0), ndigits=2)
    d["kdRatio"] = safe_ratio(d.get("kills", 0), max(1, d.get("deaths", 0)), ndigits=3)

    wins = _int(d.get("wins", 0))
    loses = _int(d.get("loses", 0))
    total = wins + loses
    d["winPercent"] = round((wins / total) * 100.0, 2) if total > 0 else 0.0
    return d


# ---- group delta engine ----
def diff_list_by_id(
    old_list: Optional[List[Dict[str, Any]]],
    new_list: Optional[List[Dict[str, Any]]],
    *,
    name_fields: List[str],
    field_map: Dict[str, Union[str, Tuple[str, str]]],
    derive_kpm: Optional[Tuple[str, str]] = None,
    derive_kd: Optional[Tuple[str, str]] = None,
    keep_zero_rows: bool = False
) -> List[Dict[str, Any]]:
    old_map = _list_to_map_by_id(old_list or [])
    new_map = _list_to_map_by_id(new_list or [])
    ids = sorted(set(old_map.keys()) | set(new_map.keys()))

    out: List[Dict[str, Any]] = []
    for _id in ids:
        o = old_map.get(_id, {}) or {}
        n = new_map.get(_id, {}) or {}

        base_item = n if n else o
        row: Dict[str, Any] = {
            "id": base_item.get("id", _id),
            "name": _item_name(base_item, name_fields),
            "imageUrl": _item_image(base_item),
        }

        deltas = _diff_item(o, n, field_map)
        row.update(deltas)

        if derive_kpm:
            kf, sf = derive_kpm
            row["killsPerMinute"] = safe_kpm(row.get(kf, 0), row.get(sf, 0), ndigits=2)
        if derive_kd:
            kf, df = derive_kd
            row["kdRatio"] = safe_ratio(row.get(kf, 0), max(1, row.get(df, 0)), ndigits=3)

        changed = any(_int(v) != 0 for v in deltas.values())
        if changed or keep_zero_rows:
            out.append(row)

    return out


# ---- field maps (match your gametools shape; missing fields become 0) ----
FIELD_MAP_GAME_MODE = {
    "matchesPlayed": "matches",
    "matchesWon": "wins",
    "matchesLost": "losses",
    "timePlayed": "secondsPlayed",
    "kills": "kills",
    "deaths": "deaths",
    "assists": "killAssists",
    "repairs": "repairs",
    "revives": "revives",
    "spots": "spots",
    "objectiveTime": "objectiveTime",
    "objectivesCaptured": "objectivesCaptured",
    "objectivesDefended": "objectivesDefended",
    "score": "scoreIn",
    "headshotKills": "headshotKills",
    # ⚠️ dpm 是“每分钟”不是累计；建议不放 delta（默认注释掉）
    # "damage": "dpm",
}

FIELD_MAP_MAP = {
    "matchesPlayed": "matches",
    "matchesWon": "wins",
    "matchesLost": "losses",
    "timePlayed": "secondsPlayed",
}

FIELD_MAP_CLASS = {
    "kills": "kills",
    "deaths": "deaths",
    "assists": "assists",
    "timePlayed": "secondsPlayed",
    "score": "score",
    "spawns": "spawns",
}

FIELD_MAP_WEAPON = {
    "kills": "kills",
    "headshotKills": "headshotKills",
    "bodyKills": "bodyKills",
    "hipfireKills": "hipfireKills",
    "scopedKills": "scopedKills",
    "multiKills": "multiKills",
    "damage": "damage",
    "assistsDamage": "assistsDamage",
    "shotsFired": "shotsFired",
    "shotsHit": "shotsHit",
    "timeEquipped": "timeEquipped",
    "spawns": "spawns",
}

FIELD_MAP_VEHICLE = {
    "kills": "kills",
    "damage": "damage",
    "spawns": "spawns",
    "roadKills": "roadKills",
    "multiKills": "multiKills",
    "distanceTraveled": "distanceTraveled",
    "driverAssists": "driverAssists",
    "passengerAssists": "passengerAssists",
    "assists": "assists",
    "vehiclesDestroyedWith": "vehiclesDestroyedWith",
    "destroyed": "destroyed",
    "timeIn": "timeIn",
    "damageTo": "damageTo",
}

FIELD_MAP_GADGET = {
    "kills": "kills",
    "damage": "damage",
    "uses": "uses",
    "assists": "assists",
    "assistsDamage": "assistsDamage",
    "spots": "spots",
    "spotAssists": "spotAssists",
    "vehiclesDestroyedWith": "vehiclesDestroyedWith",
    "multiKills": "multiKills",
    "secondsPlayed": "secondsPlayed",
}

def compute_all_group_deltas(old_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    old_data = old_data or {}
    new_data = new_data or {}
    return {
        "gamemodes": diff_list_by_id(
            old_data.get("gameModes"), new_data.get("gameModes"),
            name_fields=["gamemodeName", "name", "title"],
            field_map=FIELD_MAP_GAME_MODE,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "gamemodeGroups": diff_list_by_id(
            old_data.get("gameModeGroups"), new_data.get("gameModeGroups"),
            name_fields=["groupName", "name", "title", "gamemodeName"],
            field_map=FIELD_MAP_GAME_MODE,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "maps": diff_list_by_id(
            old_data.get("maps"), new_data.get("maps"),
            name_fields=["mapName", "name", "title"],
            field_map=FIELD_MAP_MAP,
        ),
        "kits": diff_list_by_id(
            old_data.get("classes"), new_data.get("classes"),
            name_fields=["className", "name", "title"],
            field_map=FIELD_MAP_CLASS,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "weapons": diff_list_by_id(
            old_data.get("weapons"), new_data.get("weapons"),
            name_fields=["weaponName", "name", "title"],
            field_map=FIELD_MAP_WEAPON,
            derive_kpm=("kills", "timeEquipped"),
        ),
        "weaponGroups": diff_list_by_id(
            old_data.get("weaponGroups"), new_data.get("weaponGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_WEAPON,
            derive_kpm=("kills", "timeEquipped"),
        ),
        "vehicles": diff_list_by_id(
            old_data.get("vehicles"), new_data.get("vehicles"),
            name_fields=["vehicleName", "name", "title"],
            field_map=FIELD_MAP_VEHICLE,
            derive_kpm=("kills", "timeIn"),
        ),
        "vehicleGroups": diff_list_by_id(
            old_data.get("vehicleGroups"), new_data.get("vehicleGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_VEHICLE,
            derive_kpm=("kills", "timeIn"),
        ),
        "gadgets": diff_list_by_id(
            old_data.get("gadgets"), new_data.get("gadgets"),
            name_fields=["gadgetName", "name", "title"],
            field_map=FIELD_MAP_GADGET,
            derive_kpm=("kills", "secondsPlayed"),
        ),
        "gadgetGroups": diff_list_by_id(
            old_data.get("gadgetGroups"), new_data.get("gadgetGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_GADGET,
            derive_kpm=("kills", "secondsPlayed"),
        ),
    }


def _meta_row(key_prefix: str, row: Dict[str, Any], stats_map: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": f"{key_prefix}{row.get('id')}",
        "metadata": {
            "name": row.get("name", str(row.get("id", ""))),
            "imageUrl": row.get("imageUrl", ""),
        },
        "stats": {k: row.get(k, v) for k, v in stats_map.items()},
    }


def build_trn_like_delta_match(
    account_id: Union[str, int],
    old_data: Dict[str, Any],
    new_data: Dict[str, Any],
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Thin wrapper so legacy callers still work; real work lives in
    converter.build_trn_match which produces the full TRN match shape."""
    try:
        from .converter import build_trn_match
    except ImportError:
        from app.converter import build_trn_match  # type: ignore
    return build_trn_match(
        old_stats=old_data,
        new_stats=new_data,
        account_id=str(account_id) if account_id is not None else None,
        timestamp=timestamp,
    )


def _legacy_build_trn_like_delta_match(
    account_id: Union[str, int],
    old_data: Dict[str, Any],
    new_data: Dict[str, Any],
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Original inline builder, kept for reference only."""
    ts = timestamp or _utc_iso()

    overview = compute_overview_delta(old_data, new_data)
    groups = compute_all_group_deltas(old_data, new_data)

    segment_stats = {
        "matchesPlayed": trn_stat("Matches Played", "General", "general", overview.get("matchesPlayed", 0)),
        "matchesWon": trn_stat("Matches Won", "General", "general", overview.get("wins", 0)),
        "matchesLost": trn_stat("Matches Lost", "General", "general", overview.get("loses", 0)),
        "timePlayed": trn_stat("Time Played", "General", "general", overview.get("secondsPlayed", 0), display_type="TimeSeconds"),

        "kills": trn_stat("Kills", "Combat", "combat", overview.get("kills", 0)),
        "deaths": trn_stat("Deaths", "Combat", "combat", overview.get("deaths", 0)),
        "assists": trn_stat("Assists", "Combat", "combat", overview.get("killAssists", 0)),
        "kdRatio": trn_stat("K/D Ratio", "Combat", "combat", overview.get("kdRatio", 0.0), display_type="Ratio"),
        "killsPerMinute": trn_stat("Kills/Min", "Combat", "combat", overview.get("killsPerMinute", 0.0), display_type="Number"),

        "shotsFired": trn_stat("Shots Fired", "Combat", "combat", overview.get("shotsFired", 0)),
        "shotsHit": trn_stat("Shots Hit", "Combat", "combat", overview.get("shotsHit", 0)),
        "damage": trn_stat("Damage", "Combat", "combat", overview.get("damage", 0)),
        "revives": trn_stat("Revives", "Support", "support", overview.get("revives", 0)),
        "heals": trn_stat("Heals", "Support", "support", overview.get("heals", 0)),
        "resupplies": trn_stat("Resupplies", "Support", "support", overview.get("resupplies", 0)),
        "repairs": trn_stat("Repairs", "Support", "support", overview.get("repairs", 0)),
        "vehiclesDestroyed": trn_stat("Vehicles Destroyed", "Combat", "combat", overview.get("vehiclesDestroyed", 0)),
        "enemiesSpotted": trn_stat("Enemies Spotted", "Combat", "combat", overview.get("enemiesSpotted", 0)),
        "winPercent": trn_stat("Win %", "General", "general", overview.get("winPercent", 0.0), display_type="Percentage"),
    }

    gm_stats_map = {
        "matchesPlayed": 0, "matchesWon": 0, "matchesLost": 0, "timePlayed": 0,
        "kills": 0, "deaths": 0, "assists": 0, "kdRatio": 0.0, "killsPerMinute": 0.0,
        "score": 0, "repairs": 0, "revives": 0, "spots": 0,
        "objectiveTime": 0, "objectivesCaptured": 0, "objectivesDefended": 0,
        "headshotKills": 0,
    }
    kit_stats_map = {
        "timePlayed": 0, "kills": 0, "deaths": 0, "assists": 0,
        "kdRatio": 0.0, "killsPerMinute": 0.0,
        "score": 0, "spawns": 0,
    }
    weapon_stats_map = {
        "kills": 0, "headshotKills": 0, "bodyKills": 0, "hipfireKills": 0, "scopedKills": 0,
        "multiKills": 0, "damage": 0, "assistsDamage": 0,
        "shotsFired": 0, "shotsHit": 0, "timeEquipped": 0, "spawns": 0,
        "killsPerMinute": 0.0,
    }
    vehicle_stats_map = {
        "kills": 0, "damage": 0, "spawns": 0, "roadKills": 0, "multiKills": 0,
        "distanceTraveled": 0, "driverAssists": 0, "passengerAssists": 0, "assists": 0,
        "vehiclesDestroyedWith": 0, "destroyed": 0, "timeIn": 0,
        "killsPerMinute": 0.0,
    }
    gadget_stats_map = {
        "kills": 0, "damage": 0, "uses": 0, "assists": 0, "assistsDamage": 0,
        "spots": 0, "spotAssists": 0, "vehiclesDestroyedWith": 0, "multiKills": 0,
        "secondsPlayed": 0, "killsPerMinute": 0.0,
    }
    map_stats_map = {
        "matchesPlayed": 0, "matchesWon": 0, "matchesLost": 0, "timePlayed": 0,
    }

    segment_metadata = {
        "gamemodes":       [_meta_row("gm_",  r, gm_stats_map)     for r in groups["gamemodes"]],
        "kits":            [_meta_row("kit_", r, kit_stats_map)    for r in groups["kits"]],
        "weapons":         [_meta_row("w_",   r, weapon_stats_map) for r in groups["weapons"]],
        "vehicles":        [_meta_row("v_",   r, vehicle_stats_map)for r in groups["vehicles"]],
        "gadgets":         [_meta_row("g_",   r, gadget_stats_map) for r in groups["gadgets"]],
        "levels":            [_meta_row("map_", r, map_stats_map)    for r in groups["maps"]],
    }

    return {
        "attributes": {"type": "delta", "id": str(uuid.uuid4())},
        "metadata": {"timestamp": ts},
        "segments": [{
            "type": "overview",
            "attributes": {"accountId": str(account_id)},
            "metadata": segment_metadata,
            "expiryDate": None,
            "stats": segment_stats,
        }],
        "streams": [],
        "expiryDate": None,
    }


def build_matches_response(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"matches": matches}}


# ============================================================
# 3) StatsStorage (SQLite snapshots + matches)
# ============================================================

class StatsStorage:
    """
    SQLite:
    - snapshots: 每次抓到的“纠错后的 profile JSON”
    - matches: old/new snapshot 做差得到的 TRN-like delta match JSON
    """

    DEFAULT_DB_PATH = "data/bf6_stats.db"

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Global RLock that serializes ALL database access. SQLite only
        # supports one writer at a time, and sharing a single connection
        # across FastAPI's threadpool + the background poller causes
        # sqlite3.InterfaceError when two threads hit the connection
        # concurrently. Every public method must acquire this lock.
        self._db_lock = threading.RLock()
        # Per-identifier RLock used to serialize the read-existing → compare →
        # save-match → save-profile flow inside upsert_profile_with_delta. This
        # prevents two concurrent /profile (or poller) calls for the same user
        # from inserting duplicate delta-match rows that describe the same
        # H_old → H_new transition. The dict is intentionally never trimmed —
        # one RLock per known platformUserIdentifier is cheap (~few hundred
        # bytes) and dropping locks would re-introduce the race.
        self._user_locks: Dict[str, threading.RLock] = {}
        self._user_locks_guard = threading.Lock()
        self._init_db()

    def _get_user_lock(self, identifier: str) -> threading.RLock:
        """Return (creating on first use) the RLock dedicated to one user."""
        ident = str(identifier)
        with self._user_locks_guard:
            lk = self._user_locks.get(ident)
            if lk is None:
                lk = threading.RLock()
                self._user_locks[ident] = lk
            return lk

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_db(self):
        cur = self.conn.cursor()
        cur.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;

        -- profiles: one row per player keyed by platformUserIdentifier.
        -- Stores the current converted TRN profile JSON; overwritten on change.
        CREATE TABLE IF NOT EXISTS profiles (
            platform_user_identifier TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            name TEXT NOT NULL,
            update_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            trn_profile_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_profiles_name
        ON profiles(platform, name);

        -- matches: many rows per player, append-only delta log.
        CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY,
            platform_user_identifier TEXT NOT NULL,
            created_at TEXT NOT NULL,
            from_hash TEXT,
            to_hash TEXT NOT NULL,
            match_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_matches_user_time
        ON matches(platform_user_identifier, created_at);

        CREATE INDEX IF NOT EXISTS idx_matches_to_hash
        ON matches(platform_user_identifier, to_hash);

        -- Cached public counter snapshots. The API recalculates these on a
        -- timer instead of counting large tables on every frontend request.
        CREATE TABLE IF NOT EXISTS tracked_count_history (
            id TEXT PRIMARY KEY,
            calculated_at TEXT NOT NULL,
            players_tracked INTEGER NOT NULL,
            matches_tracked INTEGER NOT NULL,
            calculation_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tracked_count_history_time
        ON tracked_count_history(calculated_at DESC);

        -- legacy snapshots table preserved for back-compat; unused by new flow.
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            update_hash TEXT NOT NULL,
            account_id TEXT,
            data_json TEXT NOT NULL
        );

        -- Suspicion reports: one anonymous reporter can mark one player once
        -- per UTC day. Reporter identity is supplied by the API layer as a
        -- signed anonymous cookie key, not an authenticated account id.
        CREATE TABLE IF NOT EXISTS player_suspicion_reports (
            id TEXT PRIMARY KEY,
            target_platform_user_identifier TEXT NOT NULL,
            reporter_key TEXT NOT NULL,
            report_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reporter_ip_hash TEXT,
            cf_ray TEXT,
            user_agent_hash TEXT,
            UNIQUE(target_platform_user_identifier, reporter_key, report_date)
        );

        CREATE TABLE IF NOT EXISTS player_suspicion_report_types (
            report_id TEXT NOT NULL,
            type TEXT NOT NULL,
            PRIMARY KEY(report_id, type),
            FOREIGN KEY(report_id)
                REFERENCES player_suspicion_reports(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sus_reports_target_time
        ON player_suspicion_reports(target_platform_user_identifier, created_at);

        CREATE INDEX IF NOT EXISTS idx_sus_reports_reporter_time
        ON player_suspicion_reports(reporter_key, created_at);

        CREATE INDEX IF NOT EXISTS idx_sus_report_types_type
        ON player_suspicion_report_types(type);

        -- Attempt log used for application-level rate limiting. This records
        -- POST attempts, including duplicate same-day marks, so repeated spam
        -- cannot avoid limits by hitting the UNIQUE report constraint.
        CREATE TABLE IF NOT EXISTS player_suspicion_rate_events (
            id TEXT PRIMARY KEY,
            reporter_key TEXT NOT NULL,
            reporter_ip_hash TEXT,
            target_platform_user_identifier TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sus_rate_reporter_time
        ON player_suspicion_rate_events(reporter_key, created_at);

        CREATE INDEX IF NOT EXISTS idx_sus_rate_ip_time
        ON player_suspicion_rate_events(reporter_ip_hash, created_at);

        CREATE INDEX IF NOT EXISTS idx_sus_rate_target_time
        ON player_suspicion_rate_events(target_platform_user_identifier, created_at);
        """)
        self.conn.commit()

        # v0.0.4 cleanup pass: remove any duplicate match rows left over from
        # the v0.0.2 race before we try to install the UNIQUE index below.
        # Strategy: per (platform_user_identifier, COALESCE(from_hash,''),
        # to_hash) triple, keep the row with the smallest rowid (the one
        # SQLite committed first in the original race) and delete the rest.
        # This is idempotent — on a clean DB it deletes 0 rows. We only log
        # when something was actually removed so normal startup stays quiet.
        try:
            cur.execute("""
                DELETE FROM matches
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM matches
                    GROUP BY platform_user_identifier,
                             COALESCE(from_hash, ''),
                             to_hash
                )
            """)
            removed = cur.rowcount or 0
            self.conn.commit()
            if removed > 0:
                log_event("INFO", "storage.cleanup_duplicate_matches", removed=removed)
        except sqlite3.OperationalError as e:
            log_event("WARN", "storage.cleanup_duplicate_matches_skipped", error=str(e))

        # v0.0.4 cleanup pass #2: remove "zero-delta" junk matches — rows where
        # every overview counter is 0 and every per-group metadata bucket is
        # empty. These were produced when the gametools update hash flipped
        # without any real gameplay (e.g. per-class secondsPlayed jitter from
        # _apply_corrections), and they're noise on the /matches feed. We scan
        # each match_json in Python rather than relying on SQLite json_extract
        # because we need to walk a full stats dict and the expression would
        # be unwieldy. Idempotent — on a clean DB this deletes 0 rows. We
        # also keep this generous: anything with even one non-zero counter
        # OR a populated per-group bucket survives.
        try:
            cur.execute("SELECT id, match_json FROM matches")
            junk_ids: List[str] = []
            for row in cur.fetchall():
                try:
                    # v0.0.5: codec.unpack handles both the new gzip-tagged rows
                    # and any legacy raw-JSON rows that the migration pass
                    # hasn't rewritten yet (cleanup runs before the migration).
                    obj = _codec.unpack(row["match_json"])
                except Exception:
                    continue  # leave unparseable rows alone
                if _is_zero_delta_match(obj):
                    junk_ids.append(row["id"])
            if junk_ids:
                cur.executemany(
                    "DELETE FROM matches WHERE id = ?",
                    [(mid,) for mid in junk_ids],
                )
                self.conn.commit()
                log_event("INFO", "storage.cleanup_zero_delta_matches", removed=len(junk_ids))
        except sqlite3.OperationalError as e:
            log_event("WARN", "storage.cleanup_zero_delta_matches_skipped", error=str(e))

        # v0.0.4 defense-in-depth: a UNIQUE expression index on
        # (platform_user_identifier, COALESCE(from_hash,''), to_hash) ensures
        # that, even across multiple worker processes (each with its own
        # in-process locks), the same H_old → H_new transition can only be
        # persisted once per user. COALESCE is needed because SQLite treats
        # NULL as distinct in unique indexes and `from_hash` is NULL for the
        # first-seen match. The cleanup pass above guarantees this CREATE
        # succeeds even on databases that originally suffered the race; the
        # try/except remains as a final safety net.
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uidx_matches_transition
                ON matches(platform_user_identifier, COALESCE(from_hash, ''), to_hash)
            """)
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            log_event("ERROR", "storage.unique_index_failed", error=str(e))
        except sqlite3.OperationalError as e:
            log_event("WARN", "storage.unique_index_skipped", error=str(e))

        # v0.0.5 storage compression migration: rewrite every legacy raw-JSON
        # row in `matches.match_json` and `profiles.trn_profile_json` through
        # storage_codec.pack() so it lands as a gzip-compressed blob with the
        # 0x01 magic byte. Reads have already been wired through codec.unpack
        # which transparently handles both the new and the legacy shapes, so
        # the API stays correct throughout the migration; the sole effect of
        # this pass is that on-disk size shrinks by ~7× as legacy rows are
        # replaced.
        #
        # Idempotent. _codec.is_packed() returns True iff a row already has
        # the new magic byte, so on a clean v0.0.5 DB this loop walks the
        # rows once and writes nothing. On a v0.0.4 DB it rewrites every
        # row exactly once. Subsequent boots are no-ops.
        #
        # We commit per-batch (per-table) rather than per-row to keep WAL
        # churn down on big DBs without holding a single huge transaction
        # if the process is killed mid-migration. UPDATE rows by primary
        # key so we don't rely on rowid stability.
        total_rewrites = 0
        for table, key_col, blob_col in (
            ("matches",  "id",                       "match_json"),
            ("profiles", "platform_user_identifier", "trn_profile_json"),
        ):
            try:
                cur.execute(f"SELECT {key_col}, {blob_col} FROM {table}")
                rewrites: List[Tuple[bytes, str]] = []
                for row in cur.fetchall():
                    blob = row[blob_col]
                    if _codec.is_packed(blob):
                        continue
                    try:
                        obj = _codec.unpack(blob)
                    except Exception as e:
                        # Genuinely-unparseable row — leave it alone, log and
                        # move on. This is extremely unlikely (every row was
                        # written by json.dumps in v0.0.4) but we never want
                        # the migration to crash the whole startup.
                        log_event(
                            "WARN",
                            "storage.compression_decode_failed",
                            table=table,
                            column=blob_col,
                            key=row[key_col],
                            error=str(e),
                        )
                        continue
                    rewrites.append((_codec.pack(obj), row[key_col]))
                if rewrites:
                    cur.executemany(
                        f"UPDATE {table} SET {blob_col} = ? WHERE {key_col} = ?",
                        rewrites,
                    )
                    self.conn.commit()
                    total_rewrites += len(rewrites)
                    log_event(
                        "INFO",
                        "storage.compression_rewrite",
                        table=table,
                        column=blob_col,
                        rows=len(rewrites),
                    )
            except sqlite3.OperationalError as e:
                # Table missing or column missing on an unusual DB shape —
                # don't block startup, just log.
                log_event("WARN", "storage.compression_migration_skipped", table=table, error=str(e))

        # Reclaim the freed pages on disk. SQLite UPDATE leaves the bytes the
        # row used to occupy as free space inside the file — without VACUUM
        # the .db file size doesn't drop even though the logical rows shrank
        # by ~7×, which is the whole point of this migration. We only run
        # VACUUM when the migration actually rewrote something so subsequent
        # boots (where every row is already packed) stay instant.
        #
        # VACUUM rebuilds the entire DB file and needs ~2× the file size in
        # free disk during the operation. Both v0.0.4 and v0.0.5 DBs have
        # plenty of headroom for this on the production VPS (~250MB → ~50MB
        # peak during VACUUM).
        #
        # Note: VACUUM cannot run inside a transaction. We've already
        # committed every per-table batch above, so the connection is in
        # autocommit-ish state and VACUUM just works.
        if total_rewrites > 0:
            try:
                log_event("INFO", "storage.vacuum_started", reason="compression_migration")
                self.conn.execute("VACUUM")
                log_event("INFO", "storage.vacuum_done", reason="compression_migration")
            except sqlite3.OperationalError as e:
                # If VACUUM fails (e.g. DB busy, disk full) the data is
                # still correct — just the file size hasn't shrunk yet.
                # Operator can run VACUUM manually later via the sqlite3
                # CLI. Don't crash startup.
                log_event("WARN", "storage.vacuum_failed", db=self.db_path, error=str(e))

    # ---- snapshots ----
    def get_latest_snapshot(self, name: str, platform: str = "") -> Optional[Dict[str, Any]]:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT * FROM snapshots
                WHERE name=? AND platform=?
                ORDER BY captured_at DESC
                LIMIT 1
            """, (name, platform))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "name": row["name"],
                "platform": row["platform"],
                "captured_at": row["captured_at"],
                "update_hash": row["update_hash"],
                "account_id": row["account_id"],
                "data": json.loads(row["data_json"]),
            }

    def save_snapshot(
        self,
        name: str,
        platform: str,
        update_hash: str,
        data: Dict[str, Any],
        account_id: Optional[str] = None,
        captured_at: Optional[str] = None
    ) -> Dict[str, str]:
        with self._db_lock:
            captured_at = captured_at or _utc_iso()
            sid = str(uuid.uuid4())

            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO snapshots(id, name, platform, captured_at, update_hash, account_id, data_json)
                VALUES(?,?,?,?,?,?,?)
            """, (
                sid, name, platform, captured_at, update_hash,
                str(account_id) if account_id is not None else None,
                json.dumps(data, ensure_ascii=False)
            ))
            self.conn.commit()
            return {"id": sid, "captured_at": captured_at}

    # ---- matches ----
    def _match_exists_for_hash(self, name: str, platform: str, to_hash: str) -> bool:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT 1 FROM matches
                WHERE name=? AND platform=? AND to_hash=?
                LIMIT 1
            """, (name, platform, to_hash))
            return cur.fetchone() is not None

    def save_match(
        self,
        name: str,
        platform: str,
        match_obj: Dict[str, Any],
        to_hash: str,
        from_hash: Optional[str] = None,
        account_id: Optional[str] = None,
        created_at: Optional[str] = None
    ) -> str:
        with self._db_lock:
            created_at = created_at or _utc_iso()
            mid = str(uuid.uuid4())

            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO matches(id, name, platform, created_at, from_hash, to_hash, account_id, match_json)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                mid, name, platform, created_at,
                from_hash, to_hash,
                str(account_id) if account_id is not None else None,
                _codec.pack(match_obj),
            ))
            self.conn.commit()
            return mid

    def count_matches(self, name: str, platform: str = "") -> int:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM matches WHERE name=? AND platform=?",
                (name, platform),
            )
            row = cur.fetchone()
            return int(row["n"]) if row else 0

    def list_matches(self, name: str, platform: str = "", limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT match_json FROM matches
                WHERE name=? AND platform=?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (name, platform, limit, offset))
            rows = cur.fetchall()
            return [_codec.unpack(r["match_json"]) for r in rows]

    def build_matches_response(self, name: str, platform: str = "", limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        return {"data": {"matches": self.list_matches(name, platform, limit, offset)}}

    # ---- one-shot: snapshot + delta match ----
    def upsert_with_delta(
        self,
        *,
        name: str,
        platform: str,
        account_id: Optional[Union[str, int]],
        update_hash: str,
        new_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        with self._db_lock:
            last = self.get_latest_snapshot(name, platform)
            now_iso = _utc_iso()

            # no change
            if last and last["update_hash"] == update_hash:
                return {
                    "changed": False,
                    "snapshotSaved": False,
                    "matchSaved": False,
                    "toHash": update_hash,
                    "fromHash": last["update_hash"],
                    "createdAt": now_iso,
                }

            # save new snapshot
            self.save_snapshot(
                name=name,
                platform=platform,
                update_hash=update_hash,
                data=new_data,
                account_id=str(account_id) if account_id is not None else None,
                captured_at=now_iso
            )

            match_saved = False
            from_hash = last["update_hash"] if last else None

            # create delta match if we have previous snapshot
            if last and not self._match_exists_for_hash(name, platform, update_hash):
                delta_match = build_trn_like_delta_match(
                    account_id=str(account_id) if account_id is not None else (last.get("account_id") or ""),
                    old_data=last["data"],
                    new_data=new_data,
                    timestamp=now_iso
                )
                self.save_match(
                    name=name,
                    platform=platform,
                    match_obj=delta_match,
                    from_hash=from_hash,
                    to_hash=update_hash,
                    account_id=str(account_id) if account_id is not None else None,
                    created_at=now_iso
                )
                match_saved = True

            return {
                "changed": True,
                "snapshotSaved": True,
                "matchSaved": match_saved,
                "toHash": update_hash,
                "fromHash": from_hash,
                "createdAt": now_iso,
            }

    # ============================================================
    # NEW: profile-keyed store + delta (TRN-profile shape)
    # ============================================================

    def get_profile(self, platform_user_identifier: str) -> Optional[Dict[str, Any]]:
        """Return the currently-stored TRN profile for this user, or None."""
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM profiles WHERE platform_user_identifier=? LIMIT 1",
                (str(platform_user_identifier),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "platformUserIdentifier": row["platform_user_identifier"],
                "platform":                row["platform"],
                "name":                    row["name"],
                "updateHash":              row["update_hash"],
                "updatedAt":               row["updated_at"],
                "trnProfile":              _codec.unpack(row["trn_profile_json"]),
            }

    def list_profiles(self) -> List[Dict[str, Any]]:
        """Return every stored profile row — used by the background poller to
        decide who to refresh. Only includes the lightweight header columns;
        skip the full TRN JSON blob."""
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT platform_user_identifier, platform, name, update_hash, updated_at
                FROM profiles
                ORDER BY updated_at ASC
            """)
            return [
                {
                    "platformUserIdentifier": r["platform_user_identifier"],
                    "platform":                r["platform"],
                    "name":                    r["name"],
                    "updateHash":              r["update_hash"],
                    "updatedAt":               r["updated_at"],
                }
                for r in cur.fetchall()
            ]

    def upsert_profile(
        self,
        *,
        platform_user_identifier: str,
        platform: str,
        name: str,
        update_hash: str,
        trn_profile: Dict[str, Any],
        updated_at: Optional[str] = None,
    ) -> None:
        """Insert or overwrite the profile row for this identifier."""
        with self._db_lock:
            updated_at = updated_at or _utc_iso()
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO profiles(platform_user_identifier, platform, name, update_hash, updated_at, trn_profile_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(platform_user_identifier) DO UPDATE SET
                    platform         = excluded.platform,
                    name             = excluded.name,
                    update_hash      = excluded.update_hash,
                    updated_at       = excluded.updated_at,
                    trn_profile_json = excluded.trn_profile_json
            """, (
                str(platform_user_identifier),
                platform,
                name,
                update_hash,
                updated_at,
                _codec.pack(trn_profile),
            ))
            self.conn.commit()

    def save_profile_match(
        self,
        *,
        platform_user_identifier: str,
        match_obj: Dict[str, Any],
        to_hash: str,
        from_hash: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """Append a delta match for this identifier.

        Returns (match_id, inserted) where:
          * match_id  — the row's id (the proposed id if it was inserted, or
                        the pre-existing row's id if dedup kicked in)
          * inserted  — True if a new row was written, False if the unique
                        transition index detected this exact H_old → H_new
                        had already been recorded (race-loser path).

        v0.0.4: switched from INSERT OR REPLACE to INSERT OR IGNORE so a
        concurrent duplicate cannot overwrite the original match's id.
        """
        with self._db_lock:
            created_at = created_at or _utc_iso()
            ident = str(platform_user_identifier)
            # prefer the match's own id so list_profile_matches returns the same id
            mid = ((match_obj.get("attributes") or {}).get("id")) or str(uuid.uuid4())
            cur = self.conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO matches(id, platform_user_identifier, created_at, from_hash, to_hash, match_json)
                VALUES(?,?,?,?,?,?)
            """, (
                str(mid),
                ident,
                created_at,
                from_hash,
                to_hash,
                _codec.pack(match_obj),
            ))
            inserted = cur.rowcount > 0
            if not inserted:
                # The UNIQUE(platform_user_identifier, COALESCE(from_hash,''), to_hash)
                # index rejected our row — another worker already saved this exact
                # transition. Look up the original match's id so callers still get
                # something to reference. COALESCE on the lookup must mirror the
                # index's COALESCE so a NULL from_hash matches the '' index entry.
                cur.execute("""
                    SELECT id FROM matches
                    WHERE platform_user_identifier = ?
                      AND COALESCE(from_hash, '') = COALESCE(?, '')
                      AND to_hash = ?
                    LIMIT 1
                """, (ident, from_hash, to_hash))
                row = cur.fetchone()
                if row and row["id"]:
                    mid = str(row["id"])
            self.conn.commit()
            return str(mid), inserted

    def list_profile_matches(
        self,
        platform_user_identifier: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT match_json FROM matches
                WHERE platform_user_identifier=?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (str(platform_user_identifier), limit, offset))
            return [_codec.unpack(r["match_json"]) for r in cur.fetchall()]

    def count_profile_matches(self, platform_user_identifier: str) -> int:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM matches WHERE platform_user_identifier=?",
                (str(platform_user_identifier),),
            )
            row = cur.fetchone()
            return int(row["n"]) if row else 0

    # ---- tracked count snapshots ----

    def count_tracked_players(self) -> int:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM profiles")
            row = cur.fetchone()
            return int(row["n"]) if row else 0

    def count_tracked_matches(self) -> int:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM matches")
            row = cur.fetchone()
            return int(row["n"]) if row else 0

    def save_tracked_count_snapshot(
        self,
        *,
        calculated_at: str,
        players_tracked: int,
        matches_tracked: int,
        calculation_ms: int,
    ) -> Dict[str, Any]:
        with self._db_lock:
            snapshot_id = str(uuid.uuid4())
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO tracked_count_history(
                    id,
                    calculated_at,
                    players_tracked,
                    matches_tracked,
                    calculation_ms
                )
                VALUES(?,?,?,?,?)
            """, (
                snapshot_id,
                calculated_at,
                int(players_tracked),
                int(matches_tracked),
                int(calculation_ms),
            ))
            self.conn.commit()
            return {
                "id": snapshot_id,
                "calculatedAt": calculated_at,
                "playersTracked": int(players_tracked),
                "matchesTracked": int(matches_tracked),
                "calculationMs": int(calculation_ms),
            }

    def get_latest_tracked_count_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT id, calculated_at, players_tracked, matches_tracked, calculation_ms
                FROM tracked_count_history
                ORDER BY calculated_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "calculatedAt": row["calculated_at"],
                "playersTracked": int(row["players_tracked"]),
                "matchesTracked": int(row["matches_tracked"]),
                "calculationMs": int(row["calculation_ms"]),
            }

    # ---- player suspicion reports ----

    def check_and_record_suspicion_attempt(
        self,
        *,
        reporter_key: str,
        reporter_ip_hash: Optional[str],
        target_platform_user_identifier: str,
        now_iso: str,
        minute_cutoff_iso: str,
        hour_cutoff_iso: str,
        day_cutoff_iso: str,
        reporter_hour_limit: int,
        reporter_day_limit: int,
        ip_hour_limit: int,
        target_minute_limit: int,
    ) -> Optional[Dict[str, Any]]:
        """Return a rate-limit dict if this suspicion POST should be blocked.

        The DB uniqueness constraint handles the one-mark-per-day rule. This
        method protects the route from high-volume attempts, including duplicate
        same-day POSTs that would otherwise never create report rows.
        """
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute(
                "DELETE FROM player_suspicion_rate_events WHERE created_at < ?",
                (day_cutoff_iso,),
            )

            def count(sql: str, args: Tuple[Any, ...]) -> int:
                cur.execute(sql, args)
                row = cur.fetchone()
                return int(row["n"]) if row else 0

            reporter_hour = count("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_rate_events
                WHERE reporter_key = ? AND created_at >= ?
            """, (reporter_key, hour_cutoff_iso))
            if reporter_hour >= reporter_hour_limit:
                self.conn.commit()
                return {"scope": "reporter_hour", "retryAfterSec": 3600}

            reporter_day = count("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_rate_events
                WHERE reporter_key = ? AND created_at >= ?
            """, (reporter_key, day_cutoff_iso))
            if reporter_day >= reporter_day_limit:
                self.conn.commit()
                return {"scope": "reporter_day", "retryAfterSec": 86400}

            if reporter_ip_hash:
                ip_hour = count("""
                    SELECT COUNT(*) AS n
                    FROM player_suspicion_rate_events
                    WHERE reporter_ip_hash = ? AND created_at >= ?
                """, (reporter_ip_hash, hour_cutoff_iso))
                if ip_hour >= ip_hour_limit:
                    self.conn.commit()
                    return {"scope": "ip_hour", "retryAfterSec": 3600}

            target_minute = count("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_rate_events
                WHERE target_platform_user_identifier = ? AND created_at >= ?
            """, (str(target_platform_user_identifier), minute_cutoff_iso))
            if target_minute >= target_minute_limit:
                self.conn.commit()
                return {"scope": "target_minute", "retryAfterSec": 60}

            cur.execute("""
                INSERT INTO player_suspicion_rate_events(
                    id,
                    reporter_key,
                    reporter_ip_hash,
                    target_platform_user_identifier,
                    created_at
                )
                VALUES(?,?,?,?,?)
            """, (
                str(uuid.uuid4()),
                reporter_key,
                reporter_ip_hash,
                str(target_platform_user_identifier),
                now_iso,
            ))
            self.conn.commit()
            return None

    def create_suspicion_report(
        self,
        *,
        target_platform_user_identifier: str,
        reporter_key: str,
        report_date: str,
        types: List[str],
        created_at: str,
        reporter_ip_hash: Optional[str],
        cf_ray: Optional[str],
        user_agent_hash: Optional[str],
    ) -> Dict[str, Any]:
        """Create one daily suspicion report, or return the existing same-day row."""
        with self._db_lock:
            ident = str(target_platform_user_identifier)
            report_id = str(uuid.uuid4())
            clean_types = sorted(set(t for t in types if t))
            cur = self.conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO player_suspicion_reports(
                    id,
                    target_platform_user_identifier,
                    reporter_key,
                    report_date,
                    created_at,
                    reporter_ip_hash,
                    cf_ray,
                    user_agent_hash
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                report_id,
                ident,
                reporter_key,
                report_date,
                created_at,
                reporter_ip_hash,
                cf_ray,
                user_agent_hash,
            ))
            created = cur.rowcount > 0

            if created and clean_types:
                cur.executemany("""
                    INSERT OR IGNORE INTO player_suspicion_report_types(report_id, type)
                    VALUES(?,?)
                """, [(report_id, t) for t in clean_types])
            elif not created:
                cur.execute("""
                    SELECT id
                    FROM player_suspicion_reports
                    WHERE target_platform_user_identifier = ?
                      AND reporter_key = ?
                      AND report_date = ?
                    LIMIT 1
                """, (ident, reporter_key, report_date))
                row = cur.fetchone()
                if row and row["id"]:
                    report_id = str(row["id"])

            self.conn.commit()
            return {
                "reportId": report_id,
                "created": created,
                "alreadyMarkedToday": not created,
                "types": clean_types,
            }

    def get_suspicion_summary(
        self,
        target_platform_user_identifier: str,
        *,
        today: str,
        last7_start: str,
    ) -> Dict[str, Any]:
        with self._db_lock:
            ident = str(target_platform_user_identifier)
            cur = self.conn.cursor()

            cur.execute("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_reports
                WHERE target_platform_user_identifier = ?
                  AND report_date = ?
            """, (ident, today))
            today_count = int((cur.fetchone() or {"n": 0})["n"])

            cur.execute("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_reports
                WHERE target_platform_user_identifier = ?
                  AND report_date >= ?
            """, (ident, last7_start))
            last7_count = int((cur.fetchone() or {"n": 0})["n"])

            cur.execute("""
                SELECT COUNT(*) AS n
                FROM player_suspicion_reports
                WHERE target_platform_user_identifier = ?
            """, (ident,))
            total_count = int((cur.fetchone() or {"n": 0})["n"])

            cur.execute("""
                SELECT t.type, COUNT(*) AS n
                FROM player_suspicion_report_types t
                JOIN player_suspicion_reports r ON r.id = t.report_id
                WHERE r.target_platform_user_identifier = ?
                GROUP BY t.type
                ORDER BY n DESC, t.type ASC
            """, (ident,))
            by_type = {str(r["type"]): int(r["n"]) for r in cur.fetchall()}

            return {
                "today": today_count,
                "last7Days": last7_count,
                "total": total_count,
                "byType": by_type,
            }

    def has_suspicion_report_today(
        self,
        target_platform_user_identifier: str,
        *,
        reporter_key: str,
        report_date: str,
    ) -> bool:
        with self._db_lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT 1
                FROM player_suspicion_reports
                WHERE target_platform_user_identifier = ?
                  AND reporter_key = ?
                  AND report_date = ?
                LIMIT 1
            """, (str(target_platform_user_identifier), reporter_key, report_date))
            return cur.fetchone() is not None

    def upsert_profile_with_delta(
        self,
        *,
        trn_profile: Dict[str, Any],
        update_hash: str,
        platform: str,
        name: str,
    ) -> Dict[str, Any]:
        """One-shot: take a freshly-built TRN profile, compare its hash against
        what's stored for this platformUserIdentifier. If changed (or if this is
        the first time we've ever seen this user), overwrite the stored profile
        and append a delta match built by subtracting the old stored TRN profile
        from the new one. On first-seen, the 'old profile' is treated as empty
        so the first match contains the player's cumulative stats.
        Returns an info dict describing what happened."""
        with self._db_lock:
            try:
                from .converter import build_trn_match_from_profiles
            except ImportError:
                from app.converter import build_trn_match_from_profiles  # type: ignore

            pinfo = ((trn_profile or {}).get("data") or {}).get("platformInfo") or {}
            identifier = str(pinfo.get("platformUserIdentifier") or "").strip()
            if not identifier:
                raise ValueError("trn_profile is missing data.platformInfo.platformUserIdentifier")

            # v0.0.4 concurrency fix: serialize the entire read-existing → compare →
            # save-match → save-profile flow per user. Without this, two concurrent
            # /profile (or /refresh, or poller) calls for the same user both observe
            # `existing.updateHash == H_old` and `update_hash == H_new`, both build a
            # delta match with a fresh uuid, and both insert — producing duplicate
            # match rows for the same H_old → H_new transition. Holding the per-user
            # RLock around the whole flow guarantees the second caller re-reads the
            # already-committed H_new and short-circuits to the no-change branch.
            # (FastAPI sync routes run in a threadpool, so concurrent requests are
            # genuinely on different threads — an asyncio Lock would not be enough.)
            user_lock = self._get_user_lock(identifier)
            with user_lock:
                existing = self.get_profile(identifier)
                now_iso = _utc_iso()

                # no change: same hash as stored (this is the branch the race loser
                # falls into once the race winner has committed)
                if existing and existing["updateHash"] == update_hash:
                    # Keep metadata in sync even when stats are unchanged. Old
                    # rows in the DB may carry stale `platform`/`name` even
                    # though the underlying nucleus id is unchanged. When the
                    # hash hasn't moved we still patch name+platform to whatever
                    # just succeeded against gametools. Keyed by the unique
                    # platform_user_identifier.
                    existing_pinfo = ((existing.get("trnProfile") or {}).get("data") or {}).get("platformInfo") or {}
                    fresh_pinfo = ((trn_profile or {}).get("data") or {}).get("platformInfo") or {}
                    metadata_changed = (
                        existing["platform"] != platform
                        or existing["name"] != name
                        or existing_pinfo.get("platformSlug") != fresh_pinfo.get("platformSlug")
                        or existing_pinfo.get("platformUserHandle") != fresh_pinfo.get("platformUserHandle")
                    )
                    if metadata_changed:
                        self.upsert_profile(
                            platform_user_identifier=identifier,
                            platform=platform,
                            name=name,
                            update_hash=update_hash,
                            trn_profile=trn_profile,
                            updated_at=now_iso,
                        )
                    return {
                        "identifier":     identifier,
                        "changed":        False,
                        "firstSeen":      False,
                        "profileSaved":   bool(metadata_changed),
                        "metadataChanged": bool(metadata_changed),
                        "matchSaved":     False,
                        "toHash":         update_hash,
                        "fromHash":       existing["updateHash"],
                        "matchId":        None,
                        "updatedAt":      now_iso if metadata_changed else existing["updatedAt"],
                    }

                first_seen = existing is None
                old_profile = existing["trnProfile"] if existing else None
                from_hash = existing["updateHash"] if existing else None

                # build delta match
                match = build_trn_match_from_profiles(
                    old_profile=old_profile,
                    new_profile=trn_profile,
                    account_id=identifier,
                    timestamp=now_iso,
                )

                # v0.0.4 zero-delta guard: skip persisting a match that contains no
                # actual counter movement. This happens when the gametools update
                # hash flips for reasons unrelated to gameplay (per-class
                # secondsPlayed jitter, leaderboard recalc, etc.). We still want
                # to overwrite the stored profile so the new updateHash sticks
                # and the next genuine flip computes a correct delta — only the
                # match save is skipped. First-seen always saves so brand-new
                # users get their initial row regardless of counter values.
                zero_delta = (not first_seen) and _is_zero_delta_match(match)
                if zero_delta:
                    match_id, match_inserted = None, False
                else:
                    match_id, match_inserted = self.save_profile_match(
                        platform_user_identifier=identifier,
                        match_obj=match,
                        to_hash=update_hash,
                        from_hash=from_hash,
                        created_at=now_iso,
                    )

                # overwrite profile (always — the hash moved even if the delta is empty)
                self.upsert_profile(
                    platform_user_identifier=identifier,
                    platform=platform,
                    name=name,
                    update_hash=update_hash,
                    trn_profile=trn_profile,
                    updated_at=now_iso,
                )

                return {
                    "identifier":     identifier,
                    "changed":        True,
                    "firstSeen":      first_seen,
                    "profileSaved":   True,
                    # match_inserted=False means either: (a) zero-delta guard
                    # filtered the match out, or (b) the UNIQUE transition index
                    # caught a cross-process race. Either way the profile is safe
                    # to overwrite.
                    "matchSaved":     bool(match_inserted),
                    "zeroDelta":      bool(zero_delta),
                    "toHash":         update_hash,
                    "fromHash":       from_hash,
                    "matchId":        match_id,
                    "updatedAt":      now_iso,
                }


# ============================================================
# 4) minimal demo
# ============================================================

if __name__ == "__main__":
    PLAYER_NAME = "BiliTV-2524OFM"   # TODO: 改成你的 ID/昵称（和 gametools 要求一致）
    PLATFORM = ""

    client = GametoolsClient()
    storage = StatsStorage("bf6_stats.db")

    data = client.fetch_stats(PLAYER_NAME, PLATFORM)
    # data = client.fetch_stats_mock_old(PLAYER_NAME, PLATFORM)
    # data = client.fetch_stats_mock_new(PLAYER_NAME, PLATFORM)
    if not data:
        print("fetch failed")
        raise SystemExit(1)

    # account_id：你可以放 origin/ea id；没有就用 name
    account_id = data.get("userId") or data.get("id") or PLAYER_NAME

    update_hash = client.generate_update_hash(data)
    if not update_hash:
        print("hash failed")
        raise SystemExit(1)

    res = storage.upsert_with_delta(
        name=PLAYER_NAME,
        platform=PLATFORM,
        account_id=str(account_id),
        update_hash=update_hash,
        new_data=data
    )
    print("upsert:", res)

    # print latest matches (TRN-like)
    latest = storage.build_matches_response(PLAYER_NAME, PLATFORM, limit=3, offset=0)
    print(json.dumps(latest, ensure_ascii=False, indent=2))

    storage.close()
