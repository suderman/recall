from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def local_date_for_timestamp(timestamp: str) -> str:
    normalized = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone().date().isoformat()


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path]:
    parse_date(date)
    raw_dir = paths.raw_capture_dir("asana", date)
    return raw_dir, raw_dir / "events.jsonl"


def append_asana_envelope(
    paths: RecallPaths,
    *,
    date: str,
    envelope: dict[str, Any],
) -> Path:
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    write_jsonl(events_path, [envelope], append=True)
    return raw_dir
