from __future__ import annotations

from datetime import datetime
from pathlib import Path

from recall.storage.paths import RecallPaths

DATE_FORMAT = "%Y-%m-%d"
TELEGRAM_CURSOR_KEY = "last_update_id"


def parse_date(value: str) -> str:
    datetime.strptime(value, DATE_FORMAT)
    return value


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path]:
    parse_date(date)
    raw_dir = paths.raw_capture_dir("telegram", date)
    return raw_dir, raw_dir / "updates.jsonl"
