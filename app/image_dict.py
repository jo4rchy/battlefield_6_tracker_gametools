"""Image-URL override dictionary.

Why this exists
---------------
Gametools' CDN occasionally returns blank `image` / `altImage` fields for
weapons, vehicles, gadgets, kits, levels, gamemodes — sometimes a specific
asset is missing for hours, sometimes the response just drops the field.
When that happens our TRN-shaped output also has blank `imageUrl` fields
and the frontend renders broken tiles.

This module keeps a local override dictionary (a flat JSON file keyed by
gametools `id`, e.g. `"lvlmpaftermath": "https://your-cdn/aftermath.jpg"`)
and consults it whenever the converter needs an image URL. Three modes,
controlled via env var:

    BF6_IMAGE_DICT_MODE = "fallback"   (default)
        Use the gametools URL if it's non-empty; if blank, fall back to
        the dict; if the dict also doesn't have it, return blank.

    BF6_IMAGE_DICT_MODE = "override"
        Use the dict's URL if it's non-empty; if blank, fall back to
        gametools; if both are blank, return blank.

    BF6_IMAGE_DICT_MODE = "off"
        Ignore the dict entirely. Same as v0.0.5 behavior.

The dict file path is configurable via BF6_IMAGE_DICT_PATH (default:
`data/image_dict.json` — the same volume as the SQLite DB, so the user
edits it on the host without rebuilding). A missing or empty file is
silently treated as `{}` so the feature is dormant by default.

The dict is loaded once at import time. To pick up edits, restart the
container — `docker compose restart bf6-tracker`. Hot-reload is
deliberately not implemented; the image dict isn't a hot path and a
file watcher would be more complexity than benefit.

Generating a template
---------------------
Run `python3 scripts/gen_image_dict_template.py` against the SQLite DB
to produce a starter template containing every `id` that's currently
showing blank in your stored profiles. Save the result as
`data/image_dict.json`, fill in URLs for the assets you care about,
restart, done.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

try:
    from .logging_utils import log_event
except ImportError:
    from app.logging_utils import log_event  # type: ignore

# ---------------------------------------------------------------------------
# Config — read once at module import. Changing env after start has no effect
# until the container restarts (see module docstring).
# ---------------------------------------------------------------------------

#: Path to the JSON dictionary on disk. Default lives inside the SQLite
#: data volume so it survives container rebuilds.
DICT_PATH: str = os.environ.get("BF6_IMAGE_DICT_PATH", "data/image_dict.json")

#: One of "fallback" / "override" / "off". Lower-cased + stripped so we
#: tolerate sloppy env values.
MODE: str = (os.environ.get("BF6_IMAGE_DICT_MODE") or "fallback").strip().lower()

_VALID_MODES = ("fallback", "override", "off")
if MODE not in _VALID_MODES:
    log_event(
        "WARN",
        "image_dict.invalid_mode",
        mode=MODE,
        valid=",".join(_VALID_MODES),
        fallback="fallback",
    )
    MODE = "fallback"


# ---------------------------------------------------------------------------
# Loader — silent on missing / empty file, loud on malformed file.
# ---------------------------------------------------------------------------

def _load_dict(path: str) -> Dict[str, str]:
    """Read the JSON file at `path` and return a {id: image_url} dict.

    Missing file → return `{}` silently (feature is dormant).
    Empty file   → return `{}` silently.
    Malformed file → log to stdout and return `{}` so a typo never crashes
                    the API. A startup log line is the right escalation
                    here; the user can fix the file and restart.
    Non-string keys/values → quietly dropped.
    """
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        log_event(
            "WARN",
            "image_dict.load_failed",
            path=path,
            error=str(e),
        )
        return {}

    if not isinstance(data, dict):
        log_event(
            "WARN",
            "image_dict.invalid_shape",
            path=path,
            type=type(data).__name__,
        )
        return {}

    cleaned: Dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            cleaned[k] = v
    return cleaned


#: Module-level cache of the dict. Loaded once at import; readers see a
#: stable snapshot for the life of the process.
_OVERRIDES: Dict[str, str] = _load_dict(DICT_PATH)


# Surface a one-line summary at startup so the operator can see whether
# the feature is active without grepping for it. Quiet when nothing is
# loaded so we don't spam logs for users who never opted in.
if MODE != "off" and _OVERRIDES:
    log_event("INFO", "image_dict.loaded", overrides=len(_OVERRIDES), path=DICT_PATH, mode=MODE)
elif MODE == "off":
    log_event("INFO", "image_dict.disabled", mode=MODE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(item_id: Optional[str], gametools_url: str) -> str:
    """Return the imageUrl that should be served for `item_id`.

    `item_id` may be None (gametools didn't surface an `id`), in which case
    we have no key to look up and just return the gametools URL as-is.

    `gametools_url` is what gametools provided — possibly an empty string.

    The mode determines the priority ordering between dict and gametools:

        fallback : gametools  -> dict       -> ""
        override : dict       -> gametools  -> ""
        off      : gametools  -> ""

    All three return `""` (not None) when nothing is available, matching
    the v0.0.4 contract for blank images.
    """
    gt = gametools_url or ""

    if MODE == "off":
        return gt

    override = _OVERRIDES.get(item_id, "") if item_id else ""

    if MODE == "override":
        # Dict first, gametools as backup.
        return override or gt

    # mode == "fallback": gametools first, dict as backup.
    return gt or override


def stats() -> Dict[str, object]:
    """Lightweight introspection — useful for an /admin endpoint or for
    operator sanity-check at startup. Returns the active config + the
    number of overrides currently loaded."""
    return {
        "mode": MODE,
        "path": DICT_PATH,
        "loaded": len(_OVERRIDES),
    }
