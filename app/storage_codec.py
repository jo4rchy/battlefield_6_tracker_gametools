"""Storage codec — transparent compression for the JSON blobs that
StatsStorage writes into SQLite.

Why this exists
---------------
At v0.0.4 the `matches.match_json` and `profiles.trn_profile_json` columns
held UTF-8 JSON text. Per-row payloads averaged ~19 KB (matches) and
~648 KB (profiles), and the `matches` table is append-only — the production
DB hit ~250 MB at 200 tracked players and was on track to fill a 30 GB VPS
disk in roughly two months.

Measured on real production data, gzip level 6 brings JSON down to about
14–15% of its raw size. Compressing both blob columns drops the prod DB
from ~250 MB to ~36 MB, restoring ~7× headroom without changing any API
contract — readers decompress transparently and the JSON shape on the wire
is byte-identical.

(zstd would shave another ~3–4% off the ratio but requires a C-extension
dep. gzip is in the Python stdlib, builds nowhere, and is plenty for the
disk-size problem we're solving. The magic-byte framework below leaves
room for adding TAG_ZSTD = 0x02 later without breaking any existing rows.)

On-disk encoding
----------------
SQLite is happy to store arbitrary bytes in a TEXT column (it never validates
encoding when you pass a `bytes` object), so we don't need a schema
migration. Every blob written by this codec is prefixed with a single magic
byte so the reader can distinguish encodings without inspecting content:

    0x00  legacy explicit-raw     (TAG_RAW + utf-8 JSON; never written by us
                                   in practice but recognized for symmetry)
    0x01  gzip-compressed JSON     (TAG_GZIP — what new writes use)

`unpack()` handles both transparently, and *also* accepts the v0.0.4-shape
"raw bytes / str that parses straight as JSON" with no magic byte at all.
That last path is what every row in a freshly-restored v0.0.4 DB looks
like, and it's what makes this codec safe to deploy *before* the migration
pass runs.

During the v0.0.5 startup migration every legacy row is rewritten with
the 0x01 prefix; afterwards the codec sees only tagged rows. The legacy
read paths stay in place indefinitely as a safety net for restored
backups.

Compression level
-----------------
gzip level 6 is the Python default and gives us 14–15% ratio with negligible
encode CPU at our write rate (a few profiles per 5-minute poll). Higher
levels (8–9) would shave maybe one more percentage point at significant
CPU cost — not worth it.
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Encoding constants
# ---------------------------------------------------------------------------

#: Single-byte tag stored as the first byte of every blob written by this
#: codec. New writes always use TAG_GZIP. TAG_RAW exists for symmetry and
#: future use ("opt out of compression for this tiny row"); v0.0.4 wrote
#: no tag at all and `unpack()` recognizes that shape too.
#:
#: 0x02+ is reserved for future codecs (e.g. zstd) — switching to a stronger
#: codec would only need a new constant + branch in pack/unpack; existing
#: rows under any other tag stay readable forever.
TAG_RAW: int = 0x00   # payload after this byte is utf-8 JSON text
TAG_GZIP: int = 0x01  # payload after this byte is gzip-compressed JSON

GZIP_LEVEL: int = 6


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pack(obj: Any) -> bytes:
    """Serialize `obj` as JSON, gzip-compress it, and prefix the magic byte.

    Returns bytes safe to write into a SQLite TEXT or BLOB column. The
    Python sqlite3 driver treats a `bytes` parameter as a BLOB binding,
    which SQLite stores faithfully under either column affinity.
    """
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=GZIP_LEVEL)
    return bytes([TAG_GZIP]) + compressed


def unpack(blob: Any) -> Any:
    """Inverse of `pack`. Accepts whatever sqlite3 returns for the column —
    `bytes`, `memoryview`, or `str` — and produces the original Python object.

    Four input shapes are recognized, in priority order:

    1. New-format gzip row: starts with TAG_GZIP. Decompress + JSON decode.
    2. Legacy explicit-raw row: starts with TAG_RAW. utf-8 JSON follows.
    3. Legacy implicit-raw bytes: a `bytes` value that parses as JSON
       directly (this is what v0.0.4 wrote — no magic byte).
    4. Legacy implicit-raw str: a `str` value that parses as JSON
       (this is what sqlite returns when the column is TEXT and the value
       was originally inserted as a Python str).

    Raises ValueError if the blob is none of the above.
    """
    if blob is None:
        raise ValueError("storage_codec.unpack: received None")

    # Normalize to bytes. memoryview is what some sqlite3 versions return for
    # blob bindings; str is what we'd see for legacy TEXT-column rows.
    if isinstance(blob, memoryview):
        data: bytes = bytes(blob)
        as_str: Optional[str] = None
    elif isinstance(blob, bytes):
        data = blob
        as_str = None
    elif isinstance(blob, str):
        data = b""
        as_str = blob
    else:
        raise ValueError(
            f"storage_codec.unpack: unsupported blob type {type(blob).__name__}"
        )

    # Case 4: came back as Python str → legacy raw JSON text.
    if as_str is not None:
        return json.loads(as_str)

    # Empty bytes is never valid.
    if not data:
        raise ValueError("storage_codec.unpack: empty blob")

    # Case 1: new gzip row.
    if data[0] == TAG_GZIP:
        decompressed = gzip.decompress(data[1:])
        return json.loads(decompressed.decode("utf-8"))

    # Case 2: legacy explicit-raw row.
    if data[0] == TAG_RAW:
        return json.loads(data[1:].decode("utf-8"))

    # Case 3: implicit raw — bytes that happen to be JSON. v0.0.4 wrote rows
    # in this shape. The first byte of any JSON document is `{ [ " 0-9 - t f
    # n` (after optional whitespace) — none of which collide with TAG_GZIP or
    # TAG_RAW, so we can disambiguate purely on the leading byte.
    return json.loads(data.decode("utf-8"))


def is_packed(blob: Any) -> bool:
    """Return True iff `blob` is in the new gzip-tagged format. Used by the
    startup migration to decide whether a row needs rewriting.

    Strings and JSON-shaped bytes (no magic prefix) are reported as NOT
    packed — they're legacy rows and need to be migrated.
    """
    if isinstance(blob, memoryview):
        blob = bytes(blob)
    if not isinstance(blob, (bytes, bytearray)):
        return False
    if not blob:
        return False
    return blob[0] == TAG_GZIP
