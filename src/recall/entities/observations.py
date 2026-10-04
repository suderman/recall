"""Retain observed identity labels without publishing mutable resolution state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

LABELS_PATH = "rebuild/identity-labels.jsonl"


def observed_labels(
    paths: RecallPaths, *, database: Path | None = None, snapshot: Path | None = None
) -> dict[str, list[str]]:
    database = database if database is not None else paths.database
    snapshot = snapshot if snapshot is not None else paths.state / LABELS_PATH
    labels: dict[str, set[str]] = {}
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            for identity, value, label in db.execute(
                "SELECT identity_id,value,label FROM identities"
            ):
                labels[identity] = {item for item in (value, label) if item}
            for identity, value in db.execute("SELECT identity_id,value FROM identity_aliases"):
                labels.setdefault(identity, set()).add(value)
    if snapshot.exists():
        for row in read_jsonl(snapshot):
            try:
                identity, values = row["identity_id"], row["labels"]
                if not isinstance(identity, str) or not isinstance(values, list):
                    raise TypeError("Expected an identity string and label list")
                if not all(isinstance(value, str) for value in values):
                    raise TypeError("Expected string labels")
                labels.setdefault(identity, set()).update(values)
            except (KeyError, TypeError) as exc:
                raise ValueError(f"Invalid identity observations in {snapshot}") from exc
    return {key: sorted(values) for key, values in labels.items()}


def retain_labels(staging: RecallPaths, output: RecallPaths, events: list[dict[str, Any]]) -> None:
    ids = {
        identity
        for event in events
        for identity in [
            event.get("sender_identity_id"),
            *event.get("participant_identity_ids", []),
        ]
        if identity
    }
    if not ids:
        return
    existing = observed_labels(output)
    for identity, values in observed_labels(staging).items():
        if identity in ids:
            existing[identity] = sorted(set(existing.get(identity, [])) | set(values))
    write_jsonl(
        output.state / LABELS_PATH,
        [{"identity_id": key, "labels": existing[key]} for key in sorted(existing)],
    )
