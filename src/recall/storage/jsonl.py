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


def write_normalized_events(
    paths: RecallPaths,
    date: str,
    events: Iterable[NormalizedEvent],
    *,
    append: bool = False,
) -> Path:
    destination = normalized_events_path(paths.normalized, date)
    write_jsonl(destination, (event.to_record() for event in events), append=append)
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
