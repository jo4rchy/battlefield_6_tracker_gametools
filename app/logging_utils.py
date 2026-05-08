"""Small structured stdout logger for container-friendly operational logs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def log_event(level: str, event: str, **fields: Any) -> None:
    extras = " ".join(
        f"{key}={_format_value(value)}"
        for key, value in fields.items()
        if value is not None
    )
    suffix = f" {extras}" if extras else ""
    print(f"{_ts()} {level.upper():5} [{event}]{suffix}", flush=True)
