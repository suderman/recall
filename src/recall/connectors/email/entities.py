from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from recall.connectors.email.notmuch import (
    EmailAddress,
    NotmuchRunner,
    load_email_messages,
    parse_date,
)
from recall.entities.storage import upsert_identities, upsert_identity_aliases
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class EmailEntitySyncResult:
    identities_synced: int
    aliases_synced: int


def _identity_id(address: str) -> str:
    normalized = []
    for char in address.strip().lower():
        normalized.append(char if char.isalnum() else "_")
    compact = "".join(normalized).strip("_")
    while "__" in compact:
        compact = compact.replace("__", "_")
    return f"ident_email_{compact}"


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _looks_meaningful_alias(address: EmailAddress) -> bool:
    display_name = address.display_name.strip()
    if not display_name:
        return False
    lowered = display_name.lower()
    if lowered == address.address.lower():
        return False
    if any(token in lowered for token in ("newsletter", "notifications", "no reply", "noreply")):
        return False
    return True


def sync_email_entities(
    paths: RecallPaths,
    *,
    date: str,
    extra_query: str | None = None,
    runner: NotmuchRunner | None = None,
) -> EmailEntitySyncResult:
    parse_date(date)
    paths.ensure_directories()
    messages = load_email_messages(date=date, extra_query=extra_query, runner=runner)
    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    identity_seen: set[str] = set()
    alias_seen: set[tuple[str, str, str]] = set()

    for message in messages:
        if "conversation_candidate" not in message.tags:
            continue
        for address in message.participant_addresses:
            identity_id = _identity_id(address.address)
            if identity_id not in identity_seen:
                identity_rows.append(
                    {
                        "identity_id": identity_id,
                        "person_id": None,
                        "source": "email",
                        "kind": "email",
                        "value": address.address,
                        "label": "Email address",
                        "is_primary": False,
                        "status": "active",
                        "valid_from": None,
                        "valid_to": None,
                        "created_at": message.timestamp,
                    }
                )
                identity_seen.add(identity_id)
            if not _looks_meaningful_alias(address):
                continue
            key = (identity_id, address.display_name.strip(), "email_header_display_name")
            if key in alias_seen:
                continue
            alias_seen.add(key)
            alias_rows.append(
                {
                    "identity_alias_id": _identity_alias_id(identity_id, key[1], key[2]),
                    "identity_id": identity_id,
                    "value": key[1],
                    "source": key[2],
                    "created_at": message.timestamp,
                }
            )

    return EmailEntitySyncResult(
        identities_synced=upsert_identities(paths, identity_rows),
        aliases_synced=upsert_identity_aliases(paths, alias_rows),
    )
