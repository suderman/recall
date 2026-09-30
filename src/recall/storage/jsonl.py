from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from recall.normalize.artifacts import NormalizedArtifact
from recall.normalize.events import NormalizedEvent
from recall.storage.paths import RecallPaths


def normalized_events_path(normalized_root: Path, date: str) -> Path:
    return normalized_root / date[:4] / f"{date}.jsonl"


def write_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    append: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize before opening an append-only raw log or replacing an existing file.
    content = "".join(
        json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n" for record in records
    )
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))

    return records


def _event_sort_key(record: Mapping[str, Any]) -> tuple[datetime, str, str]:
    timestamp = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("normalized event timestamp must include a timezone")
    return (
        timestamp.astimezone(timezone.utc),
        str(record.get("source") or ""),
        str(record["event_id"]),
    )


def _merge_normalized_event_records(
    existing: Iterable[dict[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(record["event_id"]): dict(record) for record in existing}
    for record in incoming:
        by_id[str(record["event_id"])] = dict(record)
    return sorted(by_id.values(), key=_event_sort_key)


def write_normalized_events(
    paths: RecallPaths,
    date: str,
    events: Iterable[NormalizedEvent],
    *,
    append: bool = False,
    merge_existing: bool = False,
    replace_scope: tuple[str, str | None] | None = None,
) -> Path:
    """Upsert incrementally, or replace one explicitly selected source/account.

    Callers must establish complete, successful input before scoped replacement.
    Read-modify-write requires a single writer, even though replacement is atomic.
    """
    destination = normalized_events_path(paths.normalized, date)
    event_records = [event.to_record() for event in events]
    if append and (merge_existing or replace_scope is not None):
        raise ValueError("append cannot be combined with upsert or scoped replacement")
    if replace_scope is not None:
        if any((row["source"], row.get("account")) != replace_scope for row in event_records):
            raise ValueError("incoming event outside replacement scope")
    existing = []
    if (merge_existing or replace_scope is not None) and destination.exists():
        existing = read_jsonl(destination)
    if replace_scope is not None:
        existing = [row for row in existing if (row["source"], row.get("account")) != replace_scope]
    records = _merge_normalized_event_records(existing, event_records)
    write_jsonl(destination, records, append=append)
    return destination


def write_artifact_metadata(
    paths: RecallPaths,
    *,
    source: str,
    date: str,
    artifacts: Iterable[NormalizedArtifact],
    append: bool = False,
) -> Path:
    destination = paths.artifact_metadata_path(source, date)
    existing = read_jsonl(destination) if destination.exists() and not append else []
    by_id = {row["artifact_id"]: row for row in existing}
    for artifact in artifacts:
        row = artifact.to_record()
        previous = by_id.get(artifact.artifact_id)
        if previous and row["download_status"] == "not_requested":
            for field in ("local_path", "checksums", "download_status", "last_error"):
                row[field] = previous[field]
            if row["size_bytes"] is None:
                row["size_bytes"] = previous.get("size_bytes")
            row["event_ids"] = sorted(set(previous["event_ids"]) | set(row["event_ids"]))
        by_id[artifact.artifact_id] = row
    write_jsonl(destination, by_id.values(), append=append)
    return destination
