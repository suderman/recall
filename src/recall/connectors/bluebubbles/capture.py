from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class BlueBubblesCaptureResult:
    date: str
    raw_dir: Path
    events_path: Path


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_today() -> str:
    return datetime.now().astimezone().date().isoformat()


def local_date_for_timestamp(timestamp: str) -> str:
    normalized = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone().date().isoformat()


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path]:
    raw_dir = paths.raw_capture_dir("bluebubbles", date)
    return raw_dir, raw_dir / "events.jsonl"


def append_bluebubbles_event(
    paths: RecallPaths,
    *,
    account: str,
    payload: dict[str, Any],
    received_at: str | None = None,
) -> BlueBubblesCaptureResult:
    paths.ensure_directories()
    timestamp = received_at or current_timestamp()
    date = local_date_for_timestamp(timestamp)
    raw_dir, events_path = raw_capture_paths(paths, date)
    envelope = {
        "received_at": timestamp,
        "source": "bluebubbles",
        "account": account,
        "event_type": payload.get("type", "unknown"),
        "payload": payload,
    }
    write_jsonl(events_path, [envelope], append=True)
    return BlueBubblesCaptureResult(date=date, raw_dir=raw_dir, events_path=events_path)
