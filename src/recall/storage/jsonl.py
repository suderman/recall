from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
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
    mode = "a" if append else "w"

    with path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
            handle.write("\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))

    return records


def _event_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("timestamp") or ""),
        str(record.get("source") or ""),
        str(record.get("event_id") or ""),
    )


def _merge_normalized_event_records(
    existing: Iterable[dict[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    existing_order: list[str] = []
    existing_index: dict[str, int] = {}
    incoming_by_id: dict[str, dict[str, Any]] = {}
    incoming_order: list[str] = []

    for record in existing:
        event_id = str(record["event_id"])
        existing_order.append(event_id)
        existing_index[event_id] = len(merged)
        merged.append(dict(record))

    for record in incoming:
        serialized = dict(record)
        event_id = str(serialized["event_id"])
        incoming_by_id[event_id] = serialized
        incoming_order.append(event_id)

    for event_id in existing_order:
        if event_id in incoming_by_id:
            merged[existing_index[event_id]] = incoming_by_id[event_id]

    new_records = [
        incoming_by_id[event_id] for event_id in incoming_order if event_id not in existing_index
    ]
    new_records.sort(key=_event_sort_key)
    merged.extend(new_records)

    return merged


def write_normalized_events(
    paths: RecallPaths,
    date: str,
    events: Iterable[NormalizedEvent],
    *,
    append: bool = False,
    merge_existing: bool = False,
) -> Path:
    destination = normalized_events_path(paths.normalized, date)
    event_records = [event.to_record() for event in events]

    if merge_existing and destination.exists():
        merged_records = _merge_normalized_event_records(read_jsonl(destination), event_records)
        write_jsonl(destination, merged_records, append=False)
        return destination

    write_jsonl(destination, event_records, append=append)
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
    write_jsonl(destination, (artifact.to_record() for artifact in artifacts), append=append)
    return destination
