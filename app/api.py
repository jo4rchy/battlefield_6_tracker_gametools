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
import contextlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

try:
    # normal case: `uvicorn app.api:app` or `python -m app.api` from the repo root
    from . import __version__
    from .converter import build_trn_profile, build_trn_matches_response, _pick_player_card
    from .main import GametoolsClient, StatsStorage
except ImportError:
    # fallback for `python app/api.py` — inject the repo root onto sys.path
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from app import __version__
    from app.converter import build_trn_profile, build_trn_matches_response, _pick_player_card
    from app.main import GametoolsClient, StatsStorage

DB_PATH = os.environ.get("BF6_DB_PATH", StatsStorage.DEFAULT_DB_PATH)
CORS_ORIGINS = os.environ.get("BF6_CORS_ORIGINS", "*").split(",")

# --- background poller config ---------------------------------------------
POLL_ENABLED = os.environ.get("BF6_POLL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
POLL_INTERVAL = max(30, int(os.environ.get("BF6_POLL_INTERVAL_SECONDS", "300")))
POLL_STAGGER = max(0.0, float(os.environ.get("BF6_POLL_STAGGER_SECONDS", "1.0")))

client = GametoolsClient()
storage = StatsStorage(DB_PATH)

# shared state for /status and internal use
_poll_state: Dict[str, Any] = {
    "enabled":      POLL_ENABLED,
    "intervalSec":  POLL_INTERVAL,
    "staggerSec":   POLL_STAGGER,
    "lastRunAt":    None,
    "lastRunCount": 0,
    "lastRunMs":    None,
    "lastErrors":   [],
    "runs":         0,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------

def _resolve_identifier(name: str, platform: str) -> Optional[str]:
    """Find the platformUserIdentifier for (name, platform).
    Checks the stored profiles table first; falls back to a gametools fetch."""
    cur = storage.conn.cursor()
    cur.execute(
        "SELECT platform_user_identifier FROM profiles WHERE name=? AND platform=? LIMIT 1",
        (name, platform),
    )
    row = cur.fetchone()
    if row and row["platform_user_identifier"]:
        return str(row["platform_user_identifier"])
    stats = client.fetch_stats(name, platform)
    if not stats:
        return None
    iid = stats.get("userId") or stats.get("id")
    return str(iid) if iid else None


def _refresh_by_identifier(
    identifier: str,
    platform: str,
    fallback_name: str = "",
) -> Dict[str, Any]:
    """Fetch gametools stats+profile by player id, build the TRN profile, upsert.
    Returns the deltaInfo dict from upsert_profile_with_delta (plus a couple of
    status flags if the fetch failed)."""
    full = client.fetch_full_by_id(identifier, platform)
    stats = full["stats"]
    profile = full["profile"]
    if not stats:
        return {
            "identifier":   identifier,
            "platform":     platform,
            "fetched":      False,
            "changed":      False,
            "firstSeen":    False,
            "profileSaved": False,
            "matchSaved":   False,
            "error":        "fetch_failed",
        }

    # prefer the live gametools name so profile rows self-heal when the user renames
    live_name = stats.get("userName") or fallback_name or identifier
    update_hash = client.generate_update_hash(stats)
    trn = build_trn_profile(
        stats=stats,
        profile=profile,
        name=live_name,
        platform=platform,
        update_hash=update_hash,
    )
    delta_info = storage.upsert_profile_with_delta(
        trn_profile=trn,
        update_hash=update_hash,
        platform=platform,
        name=live_name,
    )
    delta_info["fetched"] = True
    return delta_info


def _poll_once() -> Dict[str, Any]:
    """Refresh every stored profile once. Intended for the background task and
    for the manual /refresh-all endpoint. Runs sequentially with a small stagger
    so we don't spray gametools with simultaneous requests."""
    started = time.monotonic()
    profiles = storage.list_profiles()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for i, p in enumerate(profiles):
        iid = p.get("platformUserIdentifier")
        if not iid:
            continue
        try:
            info = _refresh_by_identifier(iid, p.get("platform") or "steam", p.get("name") or "")
            results.append({
                "identifier":  iid,
                "name":        p.get("name"),
                "platform":    p.get("platform"),
                "changed":     bool(info.get("changed")),
                "matchSaved":  bool(info.get("matchSaved")),
                "firstSeen":   bool(info.get("firstSeen")),
                "fetched":     bool(info.get("fetched")),
            })
            if info.get("error"):
                errors.append({"identifier": iid, "error": info["error"]})
        except Exception as e:
            errors.append({"identifier": iid, "error": repr(e)})

        if POLL_STAGGER and i + 1 < len(profiles):
            time.sleep(POLL_STAGGER)

    duration_ms = int((time.monotonic() - started) * 1000)
    _poll_state.update({
        "lastRunAt":    _iso_now(),
        "lastRunCount": len(results),
        "lastRunMs":    duration_ms,
        "lastErrors":   errors,
        "runs":         int(_poll_state.get("runs") or 0) + 1,
    })
    return {
        "refreshed":  len(results),
        "errors":     errors,
        "durationMs": duration_ms,
        "results":    results,
    }


async def _poll_loop() -> None:
    """Background task: wait the configured interval, then run _poll_once once,
    forever. Swallow errors so the task never dies silently."""
    print(f"[poller] enabled={POLL_ENABLED} interval={POLL_INTERVAL}s stagger={POLL_STAGGER}s")
    # small initial delay so uvicorn finishes booting before we fire the first batch
    await asyncio.sleep(5)
    while True:
        try:
            summary = await asyncio.to_thread(_poll_once)
            print(f"[poller] refreshed={summary['refreshed']} "
                  f"errors={len(summary['errors'])} dur={summary['durationMs']}ms")
        except Exception as e:
            print(f"[poller] unexpected error: {e!r}")
        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------
# FastAPI lifespan — start / stop the background poller
# --------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task: Optional[asyncio.Task] = None
    if POLL_ENABLED:
        task = asyncio.create_task(_poll_loop(), name="bf6-poller")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


app = FastAPI(title="BF6 Tracker API", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    }


# --------------------------------------------------------------------
# /search — TRN-style search response (single result by exact name)
# --------------------------------------------------------------------

@app.get("/search")
def search(
    query: str = Query(..., description="Player name"),
    platform: str = Query("steam"),
) -> Dict[str, Any]:
    stats = client.fetch_stats(query, platform)
    profile = client.fetch_profile(query, platform)
    if not stats:
        return {"data": []}
    user_id = str(stats.get("userId") or stats.get("id") or "")
    return {
        "data": [
            {
                "platformInfo": {
                    "platformSlug": {"steam": "origin", "xbox": "xbl", "ps": "psn"}.get(platform, platform),
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
    name: Optional[str] = Query(None, description="Player name — ignored if identifier is given"),
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    platform: str = Query("steam"),
    raw: bool = Query(False, description="Return gametools raw payload instead of TRN-shaped"),
) -> Dict[str, Any]:
    if not identifier and not name:
        raise HTTPException(status_code=400, detail="Need either identifier or name")

    # prefer id-based fetch — stable across gametag / name changes
    if identifier:
        full = client.fetch_full_by_id(identifier, platform)
        existing = storage.get_profile(identifier)
        stored_name = existing["name"] if existing else None
        effective_name = (
            (full["stats"] or {}).get("userName")
            or name
            or stored_name
            or identifier
        )
    else:
        full = client.fetch_full(name, platform)
        effective_name = name

    stats = full["stats"]
    profile = full["profile"]
    if not stats:
        raise HTTPException(status_code=502, detail="Failed to fetch gametools stats")

    update_hash = client.generate_update_hash(stats)

    trn = build_trn_profile(
        stats=stats,
        profile=profile,
        name=effective_name,
        platform=platform,
        update_hash=update_hash,
    )

    delta_info = storage.upsert_profile_with_delta(
        trn_profile=trn,
        update_hash=update_hash,
        platform=platform,
        name=effective_name,
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
    name: Optional[str] = Query(None),
    platform: str = Query("steam"),
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    iid = identifier or (_resolve_identifier(name, platform) if name else None)
    if not iid:
        raise HTTPException(status_code=400, detail="Need either identifier or name")

    matches = storage.list_profile_matches(iid, limit=limit, offset=offset)
    total = storage.count_profile_matches(iid)
    next_page = (offset // limit + 2) if (offset + limit) < total else None

    return build_trn_matches_response(
        matches,
        account_id=iid,
        next_page=next_page,
    )


# --------------------------------------------------------------------
# /refresh — manually refresh one player (by identifier or by name)
# --------------------------------------------------------------------

@app.post("/refresh")
def refresh(
    identifier: Optional[str] = Query(None, description="platformUserIdentifier (preferred)"),
    name: Optional[str] = Query(None),
    platform: str = Query("steam"),
) -> Dict[str, Any]:
    iid = identifier or (_resolve_identifier(name, platform) if name else None)
    if not iid:
        raise HTTPException(status_code=400, detail="Need either identifier or name")

    existing = storage.get_profile(iid)
    fallback_name = name or (existing["name"] if existing else "")
    info = _refresh_by_identifier(iid, platform, fallback_name)
    if not info.get("fetched", True):
        raise HTTPException(status_code=502, detail=f"Gametools fetch failed for {iid}")
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
    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=False)


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
