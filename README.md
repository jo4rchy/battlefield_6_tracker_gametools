# BF6 Tracker — self-hosted TRN-shaped API backed by Gametools

**Version: 0.0.4.6**

A small FastAPI service that:

1. Fetches BF6 player stats from [api.gametools.network](https://api.gametools.network/).
2. Converts the Gametools response into a **TRN Battlefield-Tracker-compatible profile shape** (same `data.platformInfo / userInfo / segments / availableSegments` contract as `battlefieldtracker.com`).
3. Stores each player's current TRN profile in SQLite (overwritten per hash change) and appends a **delta "match"** every time the player's counters move (kills / deaths / wins / secondsPlayed, etc.).
4. **Auto-refreshes** every tracked profile in the background every 5 minutes using the stable `platformUserIdentifier` (nucleus id), so match history keeps accumulating even when nobody hits the frontend.
5. Exposes `/profile`, `/matches`, `/search`, `/refresh`, `/refresh-all`, `/profiles`, `/status`, `/ping` — ready to be consumed by a Vite + React TRN-style frontend and an OBS browser-source widget.

The whole thing is packaged into a single Docker image so you can drop it on a NAS and forget about it.

## Project layout

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # GametoolsClient + StatsStorage + delta-match builder
│   ├── converter.py   # Gametools -> TRN profile shape (overview + all segment types)
│   └── api.py         # FastAPI routes + background poller
├── data/              # SQLite DB lives here (mounted as a docker volume)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Database layout (SQLite)

Keyed by `platform_user_identifier` — the gametools nucleus id — so renaming your gametag never breaks history.

| table      | cardinality per player | behaviour          | purpose                                   |
|------------|------------------------|--------------------|-------------------------------------------|
| `profiles` | 1                      | overwritten        | current career TRN profile                |
| `matches`  | N                      | append-only        | full delta-match history (`/matches`)     |
| `snapshots`| —                      | legacy, untouched  | old name-keyed flow, unused by the new API |

## Endpoints

All routes return JSON.

| Method | Route | Description |
|---|---|---|
| GET  | `/ping`    | Health check. |
| GET  | `/status`  | Poller state (`enabled`, `intervalSec`, `lastRunAt`, `lastRunMs`, `lastErrors`, …) plus the current tracked-profile count. |
| GET  | `/profiles`| Lightweight list of every tracked player: `platformUserIdentifier`, `platform`, `name`, `updateHash`, `updatedAt`. |
| GET  | `/search?query=<name>&platform=pc` | TRN-style search wrapper. Returns a single-entry list with `platformInfo` / `userInfo`. |
| GET  | `/profile?identifier=<id>&platform=pc` *(or `?name=<name>`)* | Fetches stats + profile from Gametools (id-based fetch when `identifier` given), overwrites the stored TRN profile if `update_hash` changed, appends a delta match, and returns the full **TRN-shaped profile**. Pass `&raw=true` to return the raw Gametools payload instead. |
| GET  | `/matches?identifier=<id>&platform=pc&limit=20&offset=0` *(or `?name=<name>`)* | Paged list of locally-computed **delta matches** in TRN "matches" shape. `metadata.next` points at the next page. |
| POST | `/refresh?identifier=<id>&platform=pc` *(or `?name=<name>`)* | Force a fetch + upsert for one player. |
| POST | `/refresh-all` | Fire the background poll immediately (sequentially refreshes every row in `profiles`). Returns a summary of what changed. |

### Query-parameter preference

`identifier` (= `platformUserIdentifier`, the gametools nucleus id) is always preferred over `name` because:

- it's stable when the player renames their gametag
- the id-based gametools endpoint is faster and doesn't need name resolution

Falling back to `name` still works for first-time lookups; once a profile is stored it can be refreshed forever by identifier alone.

### Example profile response (truncated)

```json
{
  "data": {
    "platformInfo": { "platformSlug": "origin", "platformUserHandle": "BiliTV-2524OFM", "platformUserIdentifier": "1009230165587", ... },
    "userInfo":     { "badges": 146, ... },
    "metadata":     { "updateHash": "c83201" },
    "segments": [
      { "type": "overview", "stats": { "kills": { "value": 58607, "displayValue": "58,607", "displayType": "Number", ... }, ... } },
      { "type": "gamemode-category", "attributes": { "key": "gm_all" }, ... },
      { "type": "gamemode", "attributes": { "key": "gm_cq" }, ... },
      { "type": "kit", "attributes": { "key": "kit_assault" }, ... },
      { "type": "weapon", "attributes": { "key": "wp_mg_l110" }, ... },
      { "type": "vehicle", "attributes": { "key": "veh_air_panthera" }, ... },
      { "type": "gadget", "attributes": { "key": "gad_callin_airstrike" }, ... },
      { "type": "level", "attributes": { "key": "lvlmpabbasid" }, ... }
    ],
    "availableSegments": [...],
    "expiryDate": "0001-01-01T00:00:00+00:00"
  },
  "deltaInfo": { "changed": true, "firstSeen": false, "profileSaved": true, "matchSaved": true, "fromHash": "…", "toHash": "…", "matchId": "…" }
}
```

Segment types produced: `overview`, `gamemode`, `gamemode-category`, `kit`, `kit-category`, `level`, `weapon`, `weapon-category`, `vehicle`, `vehicle-category`, `gadget`, `gadget-category` — a superset of what battlefieldtracker.com exposes.

Match envelope shape is aligned 1:1 with the TRN `/matches` reference payload (`attributes {type, id}`, `metadata.timestamp`, `segments[0].stats`, `metadata.{gamemodes, kits, levels, weapons, vehicles, gadgets}`, `expiryDate`). Display types used: `Number`, `TimeSeconds`, `NumberPrecision2`, `NumberPercentage`.

## Background auto-refresh

On startup, FastAPI spawns an asyncio task that loops:

```
wait interval → refresh every profile in SQLite sequentially → repeat
```

Each refresh calls gametools with the stored `platformUserIdentifier` (`playerid=<id>&nucleus_id=<id>`) and routes the result through the same `upsert_profile_with_delta` used by `/profile`, so a delta match is appended automatically whenever counters move.

Config (env vars):

| var | default | purpose |
|---|---|---|
| `BF6_POLL_ENABLED`          | `true` | set to `false` to disable the background task |
| `BF6_POLL_INTERVAL_SECONDS` | `300`  | seconds between polls (min 30) |
| `BF6_POLL_STAGGER_SECONDS`  | `1.0`  | sleep between individual gametools calls so we don't burst them |
| `BF6_DB_PATH`               | `data/bf6_stats.db` | SQLite path |
| `BF6_CORS_ORIGINS`          | `*`    | comma-separated list of allowed origins |

Inspect poller health at any time:

```bash
curl -s http://<nas>:8000/status | jq
```

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.api:app --reload --host 0.0.0.0 --port 8000
# first-time add: name or id
curl "http://localhost:8000/profile?name=YOUR_NAME&platform=pc"
# subsequent refreshes by id (preferred)
curl "http://localhost:8000/profile?identifier=1009230165587&platform=pc"
```

## Running on a NAS with Docker

### Synology (Container Manager / DSM 7) / QNAP / unRAID

1. Copy this folder to your NAS, e.g. `/volume1/docker/bf6-tracker/`.
2. On the NAS shell:

   ```bash
   cd /volume1/docker/bf6-tracker
   docker compose up -d --build
   ```

3. The API is now on `http://<nas-ip>:8000`. The SQLite DB lives on the host at `./data/bf6_stats.db`, so it survives container restarts, image rebuilds, and NAS reboots. The background poller starts automatically and refreshes every tracked profile every 5 minutes.

4. (Optional) Put a reverse proxy in front of it — e.g. Nginx Proxy Manager — and bind it to `battlefieldtracker.joarchy.com`. Then in `docker-compose.yml`:

   ```yaml
   environment:
     BF6_CORS_ORIGINS: "https://battlefieldtracker.joarchy.com,http://localhost:5173"
   ```

### Pre-built image → NAS workflow

If you build the image on your dev box instead of on the NAS, export it and load it over SSH — see the comment block at the end of `app/api.py` for the exact commands. Short version:

```bash
# on dev box (linux/amd64 for Synology / x86 NAS)
docker buildx build --no-cache --platform linux/amd64 -t bf6-tracker:latest --load .
docker save bf6-tracker:latest -o bf6-tracker-amd64.tar

# on NAS
docker load -i bf6-tracker-amd64.tar
docker compose up -d
```

### Logs, restart, update

```bash
docker compose logs -f bf6-tracker    # follow logs (you'll see the poller lines)
docker compose restart bf6-tracker
docker compose up -d --build          # rebuild after code changes
```

## Frontend integration (Vite + React)

The frontend can talk to this API exactly like the official TRN one — same field names, same segment types, same display types (`Number`, `TimeSeconds`, `NumberPrecision2`, `NumberPercentage`).

```ts
// vite.config.ts — dev proxy to the NAS
export default defineConfig({
  server: {
    proxy: {
      "/api": { target: "http://nas.local:8000", changeOrigin: true, rewrite: p => p.replace(/^\/api/, "") },
    },
  },
});

// src/api/bf6.ts
export async function getProfile(idOrName: string, platform = "pc") {
  const key = /^\d+$/.test(idOrName) ? "identifier" : "name";
  const r = await fetch(`/api/profile?${key}=${encodeURIComponent(idOrName)}&platform=${platform}`);
  return r.json();
}
export async function getMatches(identifier: string, limit = 20, offset = 0, platform = "pc") {
  const r = await fetch(`/api/matches?identifier=${identifier}&platform=${platform}&limit=${limit}&offset=${offset}`);
  return r.json();
}
```

## OBS widget

The storage is already compatible with a streaming widget:

- `GET /matches?identifier=<id>&limit=1` returns the **last session's delta** in TRN-match shape — perfect for an OBS browser source that shows "this session: +17 kills, +2 deaths, K/D 8.5".
- Because the background poller runs every 5 minutes, a widget that polls `GET /matches?...&limit=1` every 30s will start showing the latest session within a minute of the player leaving a match — no manual `/refresh` needed.
- `POST /refresh?identifier=<id>` can still be hit from OBS-startup so the widget always begins with a fresh baseline.

## Changelog

### v0.0.4 — concurrency fix

Fixes a race in `upsert_profile_with_delta` that allowed duplicate delta-match rows when the same player was hit by multiple concurrent `/profile` (or `/refresh`) requests while the first one was still running. Two callers would both observe the stored hash as `H_old`, both compute `H_new`, both build a delta match with a fresh uuid, and both insert — producing two rows for the same `H_old → H_new` transition.

Two layers of protection added:

1. **Per-identifier `threading.RLock`** held inside `upsert_profile_with_delta`. The read-existing → compare → save-match → save-profile flow is now atomic per user, so the second caller re-reads the freshly-committed `H_new` and short-circuits to the no-change branch.
2. **Defense-in-depth UNIQUE index** `uidx_matches_transition` on `matches(platform_user_identifier, COALESCE(from_hash,''), to_hash)`. If the API is ever scaled to multiple worker processes (each with its own in-process locks), the database itself rejects the duplicate. `save_profile_match` switched from `INSERT OR REPLACE` to `INSERT OR IGNORE` and now returns `(match_id, inserted)` so the dedup outcome is surfaced via `deltaInfo.matchSaved`.

Also removes **zero-delta junk matches** — rows where every overview counter is `0` and every per-group metadata bucket (`gamemodes` / `kits` / `levels` / `weapons` / `vehicles` / `gadgets`) is empty. These appeared when the gametools update hash flipped without any real gameplay (e.g. per-class `secondsPlayed` jitter from `_apply_corrections`, leaderboard recalcs that nudge `careerPlayerRank` by ±1) and they cluttered the `/matches` feed with noise. Two protections:

1. **Startup cleanup**: `_init_db` scans `matches`, applies the predicate, deletes the junk rows, and logs the count.
2. **Write-time guard**: `upsert_profile_with_delta` now skips `save_profile_match` when the just-built delta is all-zero **and** the user is not first-seen. The profile is still overwritten so the new `updateHash` sticks; the response carries `deltaInfo.matchSaved=False` and a new `deltaInfo.zeroDelta=True` flag so callers can tell the two skip paths apart (zero-delta vs. cross-process race-loser).

Migration: `_init_db` runs three idempotent **auto-cleanup** passes —
1. delete duplicate match rows (keep earliest `rowid` per `(identifier, COALESCE(from_hash,''), to_hash)` triple),
2. delete zero-delta junk match rows,
3. install `uidx_matches_transition` (UNIQUE).

On a clean DB all three are no-ops; on an existing v0.0.2 DB each logs how many rows it removed. No manual SQL required.
