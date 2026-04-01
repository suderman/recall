from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall.connectors.bluebubbles.capture import raw_capture_paths
from recall.connectors.bluebubbles.normalize import _identity_id, _identity_kind
from recall.entities.storage import upsert_identities, upsert_identity_aliases
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class BlueBubblesEntitySyncResult:
    identities_synced: int
    aliases_synced: int


def _load_raw_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _created_at(rows: list[dict[str, Any]], date: str) -> str:
    for row in rows:
        if row.get("received_at"):
            return str(row["received_at"])
    return f"{date}T00:00:00Z"


def _message_data(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload", {})
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _observed_values(rows: list[dict[str, Any]]) -> set[str]:
    observed: set[str] = set()
    for row in rows:
        data = _message_data(row)
        handle = data.get("handle")
        if handle:
            observed.add(str(handle))
        participants = data.get("participants") or []
        if isinstance(participants, list):
            for participant in participants:
                if participant:
                    observed.add(str(participant))
    return observed


def _alias_rows(identity_id: str, data: dict[str, Any], created_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, value in (
        ("bluebubbles_chat_display_name", data.get("chatDisplayName")),
        ("bluebubbles_display_name", data.get("displayName")),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        rows.append(
            {
                "identity_alias_id": _identity_alias_id(identity_id, text, source),
                "identity_id": identity_id,
                "value": text,
                "source": source,
                "created_at": created_at,
            }
        )
    return rows


def sync_bluebubbles_entities(paths: RecallPaths, *, date: str) -> BlueBubblesEntitySyncResult:
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    if not events_path.exists():
        raise FileNotFoundError(f"Missing BlueBubbles raw capture for {date} in {raw_dir}")

    rows = _load_raw_events(events_path)
    created_at = _created_at(rows, date)
    observed = sorted(_observed_values(rows))
    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    alias_seen: set[tuple[str, str, str]] = set()

    for value in observed:
        kind = _identity_kind(value)
        identity_id = _identity_id(value)
        label_map = {
            "phone": "BlueBubbles phone",
            "email": "BlueBubbles email",
            "handle": "BlueBubbles handle",
        }
        identity_rows.append(
            {
                "identity_id": identity_id,
                "person_id": None,
                "source": "bluebubbles",
                "kind": kind,
                "value": value,
                "label": label_map[kind],
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": created_at,
            }
        )

        for row in rows:
            data = _message_data(row)
            related_values = set()
            handle = data.get("handle")
            if handle:
                related_values.add(str(handle))
            participants = data.get("participants") or []
            if isinstance(participants, list):
                related_values.update(
                    str(participant) for participant in participants if participant
                )
            if value not in related_values:
                continue
            for alias_row in _alias_rows(identity_id, data, created_at):
                key = (alias_row["identity_id"], alias_row["value"], alias_row["source"])
                if key in alias_seen:
                    continue
                alias_seen.add(key)
                alias_rows.append(alias_row)

    identities_synced = upsert_identities(paths, identity_rows)
    aliases_synced = upsert_identity_aliases(paths, alias_rows)
    return BlueBubblesEntitySyncResult(
        identities_synced=identities_synced,
        aliases_synced=aliases_synced,
    )
