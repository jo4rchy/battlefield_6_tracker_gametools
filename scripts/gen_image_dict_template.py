#!/usr/bin/env python3
"""Generate a starter `image_dict.json` template by scanning the SQLite DB
for items whose imageUrl is currently blank.

Usage
-----
Run from the repo root (one level above this script):

    python3 scripts/gen_image_dict_template.py
        # writes to data/image_dict.json.template by default

    python3 scripts/gen_image_dict_template.py --db data/bf6_stats.db \\
                                               --out data/image_dict.json.template

    python3 scripts/gen_image_dict_template.py --include-known
        # also include ids gametools currently serves with a non-blank URL,
        # prefilled — produces a full reference catalog rather than just
        # the missing set.

Output
------
A pretty-printed JSON object keyed by gametools `id`, with empty strings
for every entry. Items are grouped by entity type (kit_*, wp_*, lvl*,
gad_*, veh_*, gm_*, etc.) and sorted within each group, separated by a
single-line comment-style key for readability:

    {
      "_comment_levels": "fill in the URLs below for blank levels",
      "lvlmpaftermath": "",
      "lvlmpabbasid": "",
      "_comment_weapons": "...",
      "wp_mg_l110": "",
      ...
    }

JSON has no real comments, but the loader (`app/image_dict.py`) silently
drops keys whose value isn't a non-empty string, so these `_comment_*`
keys cost nothing at runtime — they survive a normal `cat`/diff workflow
without breaking anything.

Exit code is 0 even when nothing is found; the script always writes a
file (possibly with just `{}` and the comment shell) so a downstream
operator can still mount it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

# Make the `app/` package importable when invoked from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from app import storage_codec as codec  # noqa: E402


# ---------------------------------------------------------------------------
# Entity-type buckets — keyed by the prefix gametools uses on `id`. Order
# determines how they appear in the output template.
# ---------------------------------------------------------------------------
_BUCKETS: List[Tuple[str, str, str]] = [
    # (bucket_name,  prefix,   one-line comment for the template)
    ("levels",      "lvl",     "fill in URLs for level / map images"),
    ("gamemodes",   "gm_",     "fill in URLs for gamemode icons"),
    ("kits",        "kit_",    "fill in URLs for kit / class images"),
    ("weapons",     "wp_",     "fill in URLs for weapon renders"),
    ("vehicles",    "veh_",    "fill in URLs for vehicle renders"),
    ("gadgets",     "gad_",    "fill in URLs for gadget icons"),
]

#: Items that don't match any of the prefixes above end up in this bucket.
_OTHER_BUCKET = ("other", "fill in URLs for items not matching any standard prefix")


def _bucket_for(item_id: str) -> str:
    """Return the bucket name for an `id`, falling back to "other"."""
    for bucket, prefix, _ in _BUCKETS:
        if item_id.startswith(prefix):
            return bucket
    return _OTHER_BUCKET[0]


# ---------------------------------------------------------------------------
# Walkers — pull every {id, imageUrl} pair out of a stored TRN profile or
# match. The shape is documented in the README and produced by converter.py:
# segments[*].metadata.{kits, weapons, vehicles, gadgets, gamemodes, levels}
# is a list of {"key": <prefix>+<id>, "metadata": {"name", "imageUrl"}, ...}
# Plus segments[*].attributes.key for the segment itself, which carries the
# id directly. We try both shapes so the script tolerates schema drift.
# ---------------------------------------------------------------------------

# v0.0.4-era key prefix the converter prepends inside metadata buckets,
# stripped here so the bucket key matches the gametools `id`.
_KEY_PREFIXES = ("kit_", "w_", "wp_", "v_", "veh_", "g_", "gad_", "gm_", "map_", "lvl")


def _strip_key_prefix(k: str) -> str:
    """The converter prepends a per-bucket prefix to the metadata `key`
    (e.g. "w_wp_mg_l110") so different entity types don't collide. Strip the
    leading bucket marker so we recover the gametools `id`."""
    # Order matters — try the longer / more-specific prefixes first so we
    # don't strip "g_" off "gad_*" or "v_" off "veh_*".
    for p in ("kit_", "wp_", "veh_", "gad_", "gm_", "lvl", "map_", "w_", "v_", "g_"):
        if k.startswith(p):
            rest = k[len(p):]
            # If the remainder still carries a known full prefix, the original
            # key was double-prefixed (`w_wp_*`); peel it. Otherwise return
            # the prefix itself when the remainder doesn't look like an id.
            for full in ("kit_", "wp_", "veh_", "gad_", "gm_", "lvl"):
                if rest.startswith(full):
                    return rest
            return rest if rest else k
    return k


def _walk_metadata_buckets(obj: dict) -> Iterable[Tuple[str, str]]:
    """Yield (id, imageUrl) for every metadata-bucket entry inside a
    profile/match segment. Robust to missing or empty buckets."""
    segs = (obj or {}).get("segments") or []
    for seg in segs:
        if not isinstance(seg, dict):
            continue

        # The segment itself often advertises an item via attributes.key —
        # e.g. a "weapon" segment for wp_mg_l110 will carry imageUrl on the
        # segment metadata. We pick that up here.
        attrs = seg.get("attributes") or {}
        key = attrs.get("key")
        if isinstance(key, str):
            cleaned = _strip_key_prefix(key)
            if cleaned:
                # The image lives at metadata.imageUrl on the segment
                md = seg.get("metadata") or {}
                yield cleaned, md.get("imageUrl") or ""

        # Per-bucket metadata rows
        md = seg.get("metadata") or {}
        for bucket_name in (
            "kits", "weapons", "vehicles", "gadgets",
            "gamemodes", "levels", "maps",
        ):
            for row in md.get(bucket_name) or []:
                if not isinstance(row, dict):
                    continue
                k = row.get("key") or ""
                cleaned = _strip_key_prefix(k) if isinstance(k, str) else ""
                if not cleaned:
                    continue
                row_md = row.get("metadata") or {}
                yield cleaned, row_md.get("imageUrl") or ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("BF6_DB_PATH", "data/bf6_stats.db"),
                    help="SQLite path (default: $BF6_DB_PATH or data/bf6_stats.db)")
    ap.add_argument("--out", default="data/image_dict.json.template",
                    help="Output path for the generated template")
    ap.add_argument("--include-known", action="store_true",
                    help="Include ids whose imageUrl is currently NON-blank too "
                         "(emits a full catalog instead of only the missing set)")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"ERROR: DB not found at {args.db!r}", file=sys.stderr)
        return 2

    # For each id, track whether we ever saw it with a non-empty imageUrl.
    # If yes, the template skips it (gametools is delivering for that one);
    # if no, it lands in the template with an empty string for the operator
    # to fill in.
    seen_nonblank: Set[str] = set()
    all_ids: Set[str] = set()
    sample_url: Dict[str, str] = {}  # used when --include-known

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # Walk profiles (current state)
    n_profiles = n_matches = 0
    for r in con.execute("SELECT trn_profile_json FROM profiles"):
        try:
            obj = codec.unpack(r["trn_profile_json"])
        except Exception as e:
            print(f"  warn: skipping unparseable profile row: {e}", file=sys.stderr)
            continue
        n_profiles += 1
        for item_id, url in _walk_metadata_buckets(obj):
            all_ids.add(item_id)
            if url:
                seen_nonblank.add(item_id)
                sample_url.setdefault(item_id, url)

    # And matches (richer history)
    for r in con.execute("SELECT match_json FROM matches"):
        try:
            obj = codec.unpack(r["match_json"])
        except Exception as e:
            print(f"  warn: skipping unparseable match row: {e}", file=sys.stderr)
            continue
        n_matches += 1
        for item_id, url in _walk_metadata_buckets(obj):
            all_ids.add(item_id)
            if url:
                seen_nonblank.add(item_id)
                sample_url.setdefault(item_id, url)

    con.close()

    if args.include_known:
        target_ids = all_ids
    else:
        target_ids = all_ids - seen_nonblank

    # Bucket + sort for stable, readable output
    by_bucket: Dict[str, List[str]] = defaultdict(list)
    for item_id in target_ids:
        by_bucket[_bucket_for(item_id)].append(item_id)
    for ids in by_bucket.values():
        ids.sort()

    # Build the ordered output dict. Comments are JSON keys prefixed with
    # `_comment_` — image_dict.py drops any value that isn't a non-empty
    # string, so they round-trip safely without doing anything at runtime.
    out: Dict[str, str] = {}
    for bucket, _prefix, comment in (*_BUCKETS, ("other", "", _OTHER_BUCKET[1])):
        ids = by_bucket.get(bucket) or []
        if not ids:
            continue
        out[f"_comment_{bucket}"] = comment
        for item_id in ids:
            # When --include-known we prefill the gametools URL we saw most
            # recently, so the operator gets a working baseline to edit.
            out[item_id] = sample_url.get(item_id, "") if args.include_known else ""

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")

    # Friendly summary on stderr (so > out.json doesn't capture it).
    real_count = sum(1 for k in out if not k.startswith("_comment_"))
    print(
        f"scanned {n_profiles} profile(s) + {n_matches} match(es); "
        f"wrote {real_count} id(s) to {args.out}",
        file=sys.stderr,
    )
    print(
        f"  ({len(seen_nonblank)} ids currently have a gametools URL, "
        f"{len(all_ids - seen_nonblank)} are blank)",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
