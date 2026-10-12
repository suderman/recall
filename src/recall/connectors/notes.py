"""Author notes: first-hand additions and corrections written per day in Org.

Notes live outside the archive, like Maildir and calendars, and are read on
each replay. Each blank-line separated paragraph becomes one cited event.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import date as Date
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_normalized_events
from recall.storage.paths import RecallPaths

# Org metadata and heading-only lines frame a note file; they are not notes.
_FRAMING = re.compile(r"^(#\+\w+:.*|\*+\s.*|:\w+:.*)$")


def notes_directory(paths: RecallPaths) -> Path:
    config = paths.sources_config / "notes.toml"
    if not config.is_file():
        raise ValueError(f"Author notes need {config} with a directory setting")
    directory = tomllib.loads(config.read_text(encoding="utf-8")).get("directory")
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError(f"Set directory in {config}")
    return Path(directory).expanduser()


def note_path(directory: Path, day: str) -> Path:
    return directory / day[:4] / day[5:7] / f"{day}.org"


def _paragraphs(text: str) -> list[str]:
    paragraphs, current = [], []
    for line in text.splitlines():
        if not line.strip():
            if current:
                paragraphs.append("\n".join(current))
                current = []
        elif not _FRAMING.match(line.strip()):
            current.append(line.rstrip())
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def normalize_notes_day(
    paths: RecallPaths,
    *,
    date: str,
    account: str = "default",
    timezone_name: str = "UTC",
    directory: Path | None = None,
) -> Path | None:
    """Replay passes ``directory`` from its input workspace; staging has no config."""
    path = note_path(directory or notes_directory(paths), date)
    if not path.is_file():
        return None
    # Notes describe the day as a whole; keep their order with one second apart.
    noon = datetime.combine(Date.fromisoformat(date), time(12), ZoneInfo(timezone_name))
    events = []
    for index, text in enumerate(_paragraphs(path.read_text(encoding="utf-8"))):
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = json.dumps(["author-note-v1", account, date, digest], separators=(",", ":"))
        events.append(
            NormalizedEvent(
                event_id="evt_" + hashlib.sha256(key.encode()).hexdigest()[:20],
                source="notes",
                timestamp=(noon + timedelta(seconds=index)).isoformat(),
                date=date,
                kind="author_note",
                account=account,
                text=text,
                raw_ref=RawReference(
                    source="notes",
                    path=str(path.absolute()),
                    locator={"paragraph": index + 1, "sha256": digest},
                ),
                tags=["author_note"],
            )
        )
    if not events:
        return None
    return write_normalized_events(paths, date, events, merge_existing=True)
