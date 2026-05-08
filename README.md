# BF6 Tracker — self-hosted TRN-shaped API backed by Gametools

> Current version is sourced from `app/__init__.py` (`__version__`). Bump that
> one line to cut a release — every other reference (FastAPI metadata, `/ping`
> + `/status` JSON, Docker `LABEL`, `build.sh` image tag and tarball name)
> reads from it automatically.

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

| table                            | cardinality per player | behaviour          | purpose                                      |
|----------------------------------|------------------------|--------------------|----------------------------------------------|
| `profiles`                       | 1                      | overwritten        | current career TRN profile                   |
| `matches`                        | N                      | append-only        | full delta-match history (`/matches`)        |
| `tracked_count_history`          | N                      | append-only        | cached player/match count snapshots          |
| `player_suspicion_reports`       | N                      | append-only daily  | one anonymous reporter mark per player/day   |
| `player_suspicion_report_types`  | N                      | append-only        | optional reasons like `aimbot`, `wallhack`   |
| `player_suspicion_rate_events`   | N                      | rolling cleanup    | POST attempt log for backend rate limits     |
| `snapshots`                      | —                      | legacy, untouched  | old name-keyed flow, unused by the new API   |

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
| GET  | `/tracked-counts` | Cached count summary for frontend display: players tracked, matches tracked, and when the count was calculated. |
| GET  | `/players/<id>/suspicion` | Public suspicion summary plus whether the current anonymous reporter already marked this player today. |
| GET  | `/players/<id>/suspicion/check` | Lightweight check response for `markedToday` only. |
| POST | `/players/<id>/suspicion` | Mark a player suspicious once per anonymous reporter per UTC day. Optional body: `{"types":["aimbot","wallhack"]}`. |
| POST | `/refresh?identifier=<id>&platform=pc` *(or `?name=<name>`)* | Force a fetch + upsert for one player. |
| POST | `/refresh-all` | Fire the background poll immediately (sequentially refreshes every row in `profiles`). Returns a summary of what changed. |

### Query-parameter preference

`identifier` (= `platformUserIdentifier`, the gametools nucleus id) is always preferred over `name` because:

- it's stable when the player renames their gametag
- the id-based gametools endpoint is faster and doesn't need name resolution

Falling back to `name` still works for first-time lookups; once a profile is stored it can be refreshed forever by identifier alone.

### Suspicion reports

Suspicion marking is keyed only by `platformUserIdentifier`; no platform is required because the same nucleus id can be reached through different platform aliases.

The frontend marks a player with:

```bash
curl -X POST "http://localhost:8000/players/1009230165587/suspicion" \
  -H "Content-Type: application/json" \
  -d '{"types":["aimbot","wallhack"]}'
```

`types` is optional. Allowed values default to `aimbot`, `wallhack`, `recoil`, `movement`, `boosting`, `other`; this list is configurable with `BF6_SUSPICION_TYPES`.

First mark today:

```json
{
  "data": {
    "identifier": "1009230165587",
    "summary": {
      "today": 1,
      "last7Days": 1,
      "last30Days": 1,
      "total": 1,
      "byType": {
        "aimbot": 1,
        "wallhack": 1
      }
    },
    "viewer": {
      "markedToday": true,
      "reportDate": "2026-05-06"
    },
    "markedToday": true,
    "alreadyMarkedToday": false,
    "reportDate": "2026-05-06"
  }
}
```

If the same anonymous reporter marks the same player again on the same UTC day, the API returns `alreadyMarkedToday: true` and does not create another report. There is intentionally no unmark flow.

For frontend display, prefer:

```http
GET /players/1009230165587/suspicion
```

It returns both public counts and the current viewer state:

```json
{
  "data": {
    "identifier": "1009230165587",
    "summary": {
      "today": 4,
      "last7Days": 18,
      "last30Days": 31,
      "total": 42,
      "byType": {
        "aimbot": 9,
        "wallhack": 7
      }
    },
    "viewer": {
      "markedToday": true,
      "reportDate": "2026-05-06"
    }
  }
}
```

For the player card, render the suspicion summary only when `summary.total > 0`. The useful display fields are `summary.total`, `summary.last7Days`, `summary.last30Days`, and each entry in `summary.byType`.

`GET /players/<id>/suspicion/check` is available when the frontend only needs:

```json
{
  "data": {
    "identifier": "1009230165587",
    "markedToday": true,
    "reportDate": "2026-05-06"
  }
}
```

The anonymous reporter identity is a backend-issued, HMAC-signed, HttpOnly cookie. The frontend does not send a user id. The DB still stores `reporter_key` so SQLite can enforce `UNIQUE(target_platform_user_identifier, reporter_key, report_date)`.

### Tracked counts

`GET /tracked-counts` returns cached counts for public frontend copy such as "by 20260506 1944 UTC, we have tracked 123 players, 456 matches".

```json
{
  "data": {
    "playersTracked": 123,
    "matchesTracked": 456,
    "calculatedAt": "2026-05-06T19:44:00+00:00",
    "calculatedAtDisplay": "20260506 1944 UTC",
    "calculationMs": 1,
    "intervalSec": 900,
    "historyId": "..."
  }
}
```

The API does not count large tables on every request. A background task recalculates on `BF6_TRACKED_COUNTS_INTERVAL_SECONDS` and saves every result in `tracked_count_history`. If the service starts with no cached state, the first `/tracked-counts` call loads the latest history row; if no history exists yet, it calculates once and stores that first snapshot.

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
wait interval → batch-fetch fresh stats for every tracked player → for each
player whose update_hash actually moved, fetch /bf6/profile/ and upsert →
repeat
```

Stats are fetched in chunks of up to **128 players per HTTP request** through
gametools' `POST /bf6/multiple/` endpoint. Several chunks run in parallel
(`BF6_POLL_BATCH_WORKERS`). Players whose new hash matches the stored hash
short-circuit immediately — no profile fetch, no DB write — so the per-cycle
load scales with *active* players rather than total tracked. Players whose
hash changed (or who are first-seen) go through the same
`upsert_profile_with_delta` used by `/profile`, with `/bf6/profile/` fetched
concurrently (`BF6_POLL_PROFILE_WORKERS`) for fresh rank / playerCard
metadata. The keep-alive `requests.Session` shared by all calls eliminates
the TCP/TLS handshake cost the old per-player loop paid twice per player.

Inspect poller health at any time:

```bash
curl -s http://<nas>:8000/status | jq
```

`/status.poller` exposes `lastRunAt`, `lastRunMs`, `lastRunChanged`,
`lastRunUnchanged`, `lastRunInvalid`, and the most recent `lastErrors[]`.

## Configuration (compose env vars)

Every supported variable is listed below with its default and effect. All
are optional; omit a variable to use its default. The same set works in
`compose.dev.yml`, `compose.prod.yml`, and `docker-compose.yml`.

### Storage and network

| var                          | default                | purpose |
|------------------------------|------------------------|---------|
| `BF6_DB_PATH`                | `data/bf6_stats.db`    | SQLite database path inside the container. The `./data` host mount in compose persists it across rebuilds. |
| `BF6_CORS_ORIGINS`           | `*`                    | Comma-separated allowed origins for the global `CORSMiddleware`. The literals `any`, `all`, and `*` all mean "allow any origin"; in production set the exact frontend URL(s) instead. |
| `BF6_SUSPICION_CORS_ORIGIN`  | `https://battlefield.joarchy.com` | Per-route origin lock that *only* applies to `POST /players/{id}/suspicion` — defends the report-write path even if the global CORS list is wide open. Set to empty string (`""`) to disable this extra check. |
| `TZ`                         | (container default)    | Timezone for log timestamps and the UTC-day boundaries used by suspicion reports. Compose files set `Asia/Hong_Kong` or `Europe/London`. |

### Background poller (batched stats + change-only profile fetch)

| var                          | default | purpose |
|------------------------------|---------|---------|
| `BF6_POLL_ENABLED`           | `true`  | Master switch. Set `false` to disable the auto-refresh task entirely. |
| `BF6_POLL_INTERVAL_SECONDS`  | `300`   | Seconds between full poll cycles. Floor 30s. |
| `BF6_POLL_BATCH_SIZE`        | `20`    | Players per `POST /bf6/multiple/` request. Hard-capped at 128 (gametools upstream limit), but 20 is the safer default for full stat payloads behind Cloudflare. |
| `BF6_POLL_BATCH_WORKERS`     | `4`     | Concurrent batch POSTs. Keep this modest; high fanout can trigger upstream 500/504 responses. |
| `BF6_POLL_PROFILE_WORKERS`   | `4`     | Concurrent `/bf6/profile/` fetches for players whose `update_hash` moved. Profile is per-player (no batch endpoint), so this is the main remaining bottleneck. |
| `BF6_POLL_STAGGER_SECONDS`   | `0.0`   | Delay between sequential stats batches and rescue batches. Useful when lowering `BF6_POLL_BATCH_WORKERS` to 1 for troubleshooting. |
| `BF6_POLL_LOG_BATCH_SUCCESS` | `false` | Set `true` to print one success log per stats batch while tuning poller settings. |

### Tracked-count cache

| var                                      | default | purpose |
|------------------------------------------|---------|---------|
| `BF6_TRACKED_COUNTS_ENABLED`             | `true`  | Enable the background task that caches `playersTracked` / `matchesTracked` counts for `/tracked-counts`. |
| `BF6_TRACKED_COUNTS_INTERVAL_SECONDS`    | `900`   | Seconds between count snapshots. Floor 60s. |

### Anonymous reporter cookie

| var                                | default                   | purpose |
|------------------------------------|---------------------------|---------|
| `BF6_REPORTER_COOKIE_NAME`         | `bf6_reporter_id`         | Cookie name used to identify the anonymous suspicion reporter. |
| `BF6_REPORTER_COOKIE_SECRET`       | `bf6-tracker-dev-secret`  | HMAC signing secret for the reporter cookie. **Set a stable, secret production value.** Changing it invalidates every existing cookie and lets those visitors mark the same player again on the same day. |
| `BF6_REPORTER_COOKIE_SECURE`       | `true`                    | Set `false` for plain-HTTP local dev. Browsers reject `SameSite=None` without `Secure=true`. |
| `BF6_REPORTER_COOKIE_SAMESITE`     | `none`                    | `lax`, `strict`, or `none`. Use `none` for cross-site frontend/API setups (frontend on a different origin than the API); requires `Secure=true`. |
| `BF6_REPORTER_COOKIE_MAX_AGE_SECONDS` | `34560000`            | Cookie lifetime in seconds. Default 400 days; floor 86400 (1 day). |

### Suspicion-report rate and shape limits

| var                                  | default                                            | purpose |
|--------------------------------------|----------------------------------------------------|---------|
| `BF6_SUSPICION_TYPES`                | `aimbot,wallhack,recoil,movement,boosting,other`   | Comma-separated allowlist of `types[]` values accepted on `POST /players/{id}/suspicion`. |
| `BF6_SUSPICION_MAX_TYPES`            | `3`                                                | Max number of `types` accepted per POST body. |
| `BF6_SUSPICION_REPORTER_HOUR_LIMIT`  | `30`                                               | Max POST attempts per anonymous reporter per hour. |
| `BF6_SUSPICION_REPORTER_DAY_LIMIT`   | `100`                                              | Max POST attempts per anonymous reporter per day. |
| `BF6_SUSPICION_IP_HOUR_LIMIT`        | `300`                                              | Hourly POST cap per IP hash. Generous on purpose so CGNAT / shared university networks don't get blanket-banned. |
| `BF6_SUSPICION_TARGET_MINUTE_LIMIT`  | `60`                                               | Max POST attempts against any single target player per minute (anti-pile-on). |

### Image-URL override dictionary (v0.0.6+)

| var                    | default                | purpose |
|------------------------|------------------------|---------|
| `BF6_IMAGE_DICT_PATH`  | `data/image_dict.json` | Path to a JSON file mapping gametools `id` → CDN URL. Lives in the same `data/` volume as the SQLite DB so you can edit it on the host and `docker compose restart` to reload. |
| `BF6_IMAGE_DICT_MODE`  | `fallback`             | `fallback` (gametools first, dict only when blank), `override` (dict first, gametools as backup), or `off` (ignore the dict entirely). |

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

The `build.sh` script at the repo root reads `__version__` from `app/__init__.py` and produces a correctly-tagged image plus a `bf6-tracker-amd64-v<version>.tar` tarball. Copy the tarball to the NAS and load it.

```bash
# on dev box (linux/amd64 for Synology / x86 NAS)
./build.sh                                # build + tag + save tarball

# copy + load on NAS
scp bf6-tracker-amd64-v*.tar user@nas:/volume1/docker/bf6-tracker/
ssh user@nas "cd /volume1/docker/bf6-tracker \
  && docker load -i bf6-tracker-amd64-v*.tar \
  && docker compose up -d"

# verify which version the NAS is now running
curl -s http://<nas>:8000/ping | jq .version
```

`build.sh` also accepts `--no-tar` (build the image only) and `--push <repo>` (build + push to a registry instead of saving a tarball).

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
export async function getSuspicion(identifier: string) {
  const r = await fetch(`/api/players/${encodeURIComponent(identifier)}/suspicion`, {
    credentials: "include",
  });
  return r.json();
}
export async function markSuspicious(identifier: string, types: string[] = []) {
  const r = await fetch(`/api/players/${encodeURIComponent(identifier)}/suspicion`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(types.length ? { types } : {}),
  });
  return r.json();
}
```

Suspicion endpoints need `credentials: "include"` so the browser sends/receives the anonymous reporter cookie. For deployed cross-origin frontend/API setups, do not leave `BF6_CORS_ORIGINS="*"`; set exact origins and configure cookie settings for HTTPS, for example:

```yaml
environment:
  BF6_CORS_ORIGINS: "https://battlefieldtracker.joarchy.com,http://localhost:5173"
  BF6_REPORTER_COOKIE_SECRET: "replace-with-a-long-random-secret"
  BF6_REPORTER_COOKIE_SECURE: "true"
  BF6_REPORTER_COOKIE_SAMESITE: "none"
```

When the API is behind Cloudflare, `CF-Connecting-IP` is used for abuse metadata/rate limiting if present. Only trust that header if the origin is not directly reachable except through Cloudflare; otherwise clients can spoof it.

## OBS widget

The storage is already compatible with a streaming widget:

- `GET /matches?identifier=<id>&limit=1` returns the **last session's delta** in TRN-match shape — perfect for an OBS browser source that shows "this session: +17 kills, +2 deaths, K/D 8.5".
- Because the background poller runs every 5 minutes, a widget that polls `GET /matches?...&limit=1` every 30s will start showing the latest session within a minute of the player leaving a match — no manual `/refresh` needed.
- `POST /refresh?identifier=<id>` can still be hit from OBS-startup so the widget always begins with a fresh baseline.

## Changelog

### v0.0.7.8 — batched poller (1,200 players in seconds, not 45 minutes)

The background poller no longer iterates players one-by-one. It uses gametools' `POST /bf6/multiple/?categories=multiplayer` endpoint to fetch **up to 128 players per HTTP request**, then only refreshes profile metadata for players whose `update_hash` actually changed.

- **`GametoolsClient.fetch_stats_batch_by_ids(items, chunk_size=20, max_workers=N)`** — new batch method on the client. Returns `{player_id: stats_payload}` with the same per-item correction logic as `fetch_stats_by_id`. Reuses a keep-alive `requests.Session` so per-player calls (`/profile`, `/refresh`, search) also stop paying the TCP/TLS handshake cost. Server-side 429/5xx responses are retried with short backoff, and failed chunks get one low-concurrency rescue pass before being marked failed.
- **`_poll_once` rewrite** — three explicit phases: (1) batch-fetch every tracked player's stats; (2) classify each player as *unchanged* (skip), *invalid* (record error), or *changed/first-seen* (queue); (3) run upserts (which fetch `/bf6/profile/` for fresh rank metadata) concurrently for the queued players only. Unchanged players never produce a profile fetch or a DB write.
- **New compose env knobs** — `BF6_POLL_BATCH_SIZE` (default 20, capped at 128), `BF6_POLL_BATCH_WORKERS` (default 4), `BF6_POLL_PROFILE_WORKERS` (default 4). `BF6_POLL_STAGGER_SECONDS` is now defaulted to `0.0` and applies to sequential/rescue batch requests. See *Configuration (compose env vars)* above.
- **`/status.poller` additions** — `lastRunChanged`, `lastRunUnchanged`, `lastRunInvalid`, plus the new `batchSize` / `batchWorkers` / `profileWorkers` fields so operators can confirm which settings were applied.

Measured behaviour at 1,200 tracked players: a full cycle now completes in seconds instead of ~45 minutes, and per-cycle wall time scales with how many players were *active* since the last poll, not with how many players are tracked. Output to the frontend is byte-identical — the TRN profile / matches shape is unchanged.

`compose.dev.yml` and `compose.prod.yml` were updated to drop the legacy `BF6_POLL_STAGGER_SECONDS` line and set the new `BATCH_SIZE` / `BATCH_WORKERS` / `PROFILE_WORKERS` knobs explicitly.

### v0.0.7 — anonymous suspicion reports

Adds one-way player suspicion marking:

- `POST /players/<id>/suspicion` records one anonymous reporter mark per player per UTC day.
- Optional multiple reasons are accepted as `{"types":["aimbot","wallhack"]}` and stored in `player_suspicion_report_types`.
- `GET /players/<id>/suspicion` returns public counts (`total`, `today`, `last7Days`, `last30Days`, `byType`) plus the current viewer's `markedToday` state.
- `GET /players/<id>/suspicion/check` returns the lightweight check state only.
- Anonymous reporter identity is a backend-issued HMAC-signed HttpOnly cookie; no frontend auth is required.
- Backend rate limits are tracked in `player_suspicion_rate_events` and return HTTP `429` with `Retry-After`.

Production operators should set a stable `BF6_REPORTER_COOKIE_SECRET`, configure exact `BF6_CORS_ORIGINS`, and use `credentials: "include"` in frontend fetches that call suspicion endpoints.

### v0.0.6 — image-URL override dictionary

Gametools' CDN occasionally returns blank `image` / `altImage` fields, leaving the frontend with broken tiles. v0.0.6 adds a local override dictionary so operators can fill in missing assets without rebuilding the image. Configured via `BF6_IMAGE_DICT_PATH` and `BF6_IMAGE_DICT_MODE` — see *Configuration (compose env vars)* above.

**File format** — flat JSON keyed by gametools `id`. Keys starting with `_comment_` are silently dropped at load, so you can use them as headings in the file without breaking lookups:

```json
{
  "_comment_levels": "level images",
  "lvlmpaftermath": "https://your-cdn/levels/aftermath.jpg",
  "_comment_weapons": "weapons",
  "wp_mg_l110":      "https://your-cdn/weapons/l110.png"
}
```

A missing or empty file is silently treated as `{}`, so the feature is dormant until you create one. Loaded once at module import — `docker compose restart bf6-tracker` to pick up edits.

**Coverage**: every TRN segment whose `imageUrl` flows through `converter._fmt_img` or `main._item_image` — weapons, vehicles, gadgets, kits, levels, gamemodes, and the corresponding `*-category` segments. The player rank icon (`rankImage.large/small` shape) is out of scope; if you want rank icon overrides, ask.

**Starter template generator** — `scripts/gen_image_dict_template.py` walks your SQLite DB and emits a JSON template containing only the ids whose imageUrl is currently blank, grouped by entity type:

```bash
python3 scripts/gen_image_dict_template.py
# scanned 200 profile(s) + N match(es); wrote 34 id(s) to data/image_dict.json.template
```

Inspect the template, fill in URLs for the assets you care about, save as `data/image_dict.json`, restart. `--include-known` produces a full reference catalog with current gametools URLs prefilled if you'd rather edit a complete view.

### v0.0.5 — storage compression + single-source version

The `matches.match_json` and `profiles.trn_profile_json` columns were ballooning the SQLite file: at 200 tracked players the prod DB had reached ~250MB, projected to fill a 30GB VPS disk in roughly two months. v0.0.5 transparently gzip-compresses both columns at write time and decompresses at read, with no API contract change — the JSON shape served to the frontend is byte-identical.

**Measured impact on real production data:**

| column                       | before  | after   | ratio |
|------------------------------|---------|---------|-------|
| `matches.match_json`         | 19.2 KB | 2.8 KB  | 14.6% |
| `profiles.trn_profile_json`  | 648 KB  | 28 KB   | 4.3%  |

Profiles compress dramatically better than matches because the per-segment metadata (weapon names, vehicle imageUrls, stat `displayName` / `displayCategory` / `displayType` strings) is huge and repeats across every player.

**On-disk format.** Each compressed blob carries a one-byte magic prefix (`0x01` = gzip-JSON, `0x00` reserved for explicit raw-JSON, `0x02+` reserved for future codecs). `app/storage_codec.py.unpack()` also recognizes the v0.0.4 shape (raw JSON with no prefix), so a freshly-restored v0.0.4 DB reads correctly even before the migration pass runs — the rollout is safe.

**Migration.** `_init_db()` got a fourth idempotent cleanup pass that walks `matches` and `profiles`, finds rows whose blob does not start with the magic prefix, and rewrites them through `codec.pack()`. Same idempotent shape as the existing v0.0.4 cleanup passes — runs once on the first v0.0.5 boot, no-op afterwards. Logged row counts so operators can confirm it ran.

**Single-source version.** `app/__init__.py.__version__` is now the only place the version string lives. `FastAPI(...)`, the `/ping` and `/status` JSON, the Dockerfile `LABEL`, and `build.sh`'s image tag and tarball filename all read from it. Bumping the version is one line; the new `build.sh` at the repo root automates the build/tag/save flow.

`/ping` and `/status` now return a `version` field too, so the frontend (and the upcoming UK-primary / US-failover split) can verify which build it just hit.

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
