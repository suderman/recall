from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from recall.connectors.asana.capture import parse_date, raw_capture_paths
from recall.entities.storage import upsert_identities, upsert_identity_aliases
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class AsanaEntitySyncResult:
    identities_synced: int
    aliases_synced: int


def _identity_id(kind: str, value: str) -> str:
    if kind == "user_id":
        return f"ident_asana_{value}"
    normalized = []
    for char in value.strip().lower():
        normalized.append(char if char.isalnum() else "_")
    compact = "".join(normalized).strip("_")
    while "__" in compact:
        compact = compact.replace("__", "_")
    return f"ident_asana_email_{compact}"


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _user_rows(
    user: dict[str, Any] | None, created_at: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(user, dict):
        return [], []
    identities: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    gid = str(user.get("gid") or "").strip()
    name = str(user.get("name") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    if gid:
        identity_id = _identity_id("user_id", gid)
        identities.append(
            {
                "identity_id": identity_id,
                "person_id": None,
                "source": "asana",
                "kind": "user_id",
                "value": gid,
                "label": "Asana user ID",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": created_at,
            }
        )
        if name:
            aliases.append(
                {
                    "identity_alias_id": _identity_alias_id(identity_id, name, "asana_user_name"),
                    "identity_id": identity_id,
                    "value": name,
                    "source": "asana_user_name",
                    "created_at": created_at,
                }
            )
        if email:
            aliases.append(
                {
                    "identity_alias_id": _identity_alias_id(identity_id, email, "asana_user_email"),
                    "identity_id": identity_id,
                    "value": email,
                    "source": "asana_user_email",
                    "created_at": created_at,
                }
            )
    if email:
        email_identity_id = _identity_id("email", email)
        identities.append(
            {
                "identity_id": email_identity_id,
                "person_id": None,
                "source": "asana",
                "kind": "email",
                "value": email,
                "label": "Asana user email",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": created_at,
            }
        )
    return identities, aliases


def sync_asana_entities(paths: RecallPaths, *, date: str) -> AsanaEntitySyncResult:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    if not events_path.exists():
        raise FileNotFoundError(f"Missing Asana raw capture for {date} in {raw_dir}")

    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    identity_seen: set[str] = set()
    alias_seen: set[tuple[str, str, str]] = set()

    for row in read_jsonl(events_path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        created_at = str(row.get("received_at") or f"{date}T00:00:00Z")
        users: list[dict[str, Any] | None] = []
        if row.get("event_type") in {"task", "task_completion"}:
            users.extend(
                [payload.get("created_by"), payload.get("assignee"), payload.get("completed_by")]
            )
            users.extend(item for item in payload.get("followers") or [] if isinstance(item, dict))
        elif row.get("event_type") == "task_story":
            users.extend([payload.get("actor")])
            participants = payload.get("task_participants") or {}
            if isinstance(participants, dict):
                users.extend([participants.get("assignee"), participants.get("created_by")])
                users.extend(
                    item for item in participants.get("followers") or [] if isinstance(item, dict)
                )

        for user in users:
            identities, aliases = _user_rows(user if isinstance(user, dict) else None, created_at)
            for identity in identities:
                if identity["identity_id"] in identity_seen:
                    continue
                identity_seen.add(identity["identity_id"])
                identity_rows.append(identity)
            for alias in aliases:
                key = (alias["identity_id"], alias["value"], alias["source"])
                if key in alias_seen:
                    continue
                alias_seen.add(key)
                alias_rows.append(alias)

    return AsanaEntitySyncResult(
        identities_synced=upsert_identities(paths, identity_rows),
        aliases_synced=upsert_identity_aliases(paths, alias_rows),
    )
