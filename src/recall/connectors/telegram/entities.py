from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall.connectors.telegram.capture import parse_date, raw_capture_paths
from recall.connectors.telegram.export_scope import ExportScope, export_scope
from recall.entities.storage import (
    upsert_aliases,
    upsert_identities,
    upsert_identity_aliases,
    upsert_persons,
    upsert_resolutions,
)
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class TelegramEntitySyncResult:
    persons_synced: int
    identities_synced: int
    aliases_synced: int
    person_aliases_synced: int
    resolutions_synced: int


def _load_raw_updates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                row = json.loads(line)
                row["_line_number"] = number
                rows.append(row)
    return rows


def _hash_id(prefix: str, *parts: str) -> str:
    payload = ":".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:20]}"


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    return _hash_id("ialias", identity_id, value, source)


def _alias_id(person_id: str, value: str, source: str) -> str:
    return _hash_id("alias", person_id, value, source)


def _resolution_id(identity_id: str, person_id: str, method: str) -> str:
    return _hash_id("res", identity_id, person_id, method)


def _person_id(user_id: int | str) -> str:
    return f"person_telegram_user_{user_id}"


def _user_identity_id(user_id: int | str) -> str:
    return f"ident_telegram_user_{user_id}"


def _chat_identity_id(chat_id: int | str) -> str:
    return f"ident_telegram_chat_{chat_id}"


def _username_identity_id(username: str) -> str:
    return f"ident_telegram_username_{username}"


def _phone_identity_id(phone_number: str) -> str:
    digits = phone_number.replace("+", "plus_").replace("-", "_")
    return f"ident_telegram_phone_{digits}"


def _created_at(rows: list[dict[str, Any]], date: str) -> str:
    for row in rows:
        if row.get("received_at"):
            return str(row["received_at"])
    return f"{date}T00:00:00Z"


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    return message if isinstance(message, dict) else payload


def _chat(payload: dict[str, Any]) -> dict[str, Any]:
    chat = payload.get("chat")
    return chat if isinstance(chat, dict) else {}


def _sort_name(first_name: str, last_name: str) -> str | None:
    first = first_name.strip()
    last = last_name.strip()
    if first and last:
        return f"{last}, {first}"
    if last:
        return last
    if first:
        return first
    return None


def _usernames(user: dict[str, Any]) -> list[str]:
    usernames = user.get("usernames")
    if isinstance(usernames, list):
        return [
            str(username).lstrip("@").strip() for username in usernames if str(username).strip()
        ]
    if isinstance(usernames, dict):
        values: list[str] = []
        for username in usernames.get("active_usernames") or []:
            text = str(username).lstrip("@").strip()
            if text:
                values.append(text)
        editable = str(usernames.get("editable_username") or "").lstrip("@").strip()
        if editable:
            values.append(editable)
        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped
    return []


def _display_name(user: dict[str, Any]) -> str:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first, last) if part).strip()
    if full_name:
        return full_name
    usernames = _usernames(user)
    if usernames:
        return f"@{usernames[0]}"
    return f"Telegram user {user.get('id')}"


def _identity_row(
    *,
    identity_id: str,
    person_id: str | None,
    kind: str,
    value: str,
    label: str,
    created_at: str,
    is_primary: bool = False,
) -> dict[str, Any]:
    return {
        "identity_id": identity_id,
        "person_id": person_id,
        "source": "telegram",
        "kind": kind,
        "value": value,
        "label": label,
        "is_primary": is_primary,
        "status": "active",
        "valid_from": None,
        "valid_to": None,
        "created_at": created_at,
    }


def _person_row(user: dict[str, Any], created_at: str) -> dict[str, Any]:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    display_name = _display_name(user)
    return {
        "person_id": _person_id(user["id"]),
        "display_name": display_name,
        "sort_name": _sort_name(first, last),
        "notes": "",
        "tags": [],
        "created_at": created_at,
    }


def _person_alias_row(person_id: str, value: str, source: str, created_at: str) -> dict[str, Any]:
    return {
        "alias_id": _alias_id(person_id, value, source),
        "person_id": person_id,
        "value": value,
        "source": source,
        "created_at": created_at,
    }


def _identity_alias_row(
    identity_id: str, value: str, source: str, created_at: str
) -> dict[str, Any]:
    return {
        "identity_alias_id": _identity_alias_id(identity_id, value, source),
        "identity_id": identity_id,
        "value": value,
        "source": source,
        "created_at": created_at,
    }


def _resolution_row(
    identity_id: str,
    person_id: str,
    created_at: str,
    *,
    method: str,
    confidence: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "resolution_id": _resolution_id(identity_id, person_id, method),
        "identity_id": identity_id,
        "person_id": person_id,
        "confidence": confidence,
        "method": method,
        "evidence": evidence,
        "created_at": created_at,
    }


def _export_observations(
    scope: ExportScope, payload: dict[str, Any], created_at: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    person_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    observations: dict[tuple[str, str], list[str]] = {}
    for user in payload.get("users") or []:
        if not isinstance(user, dict) or user.get("id") is None:
            continue
        user_id = str(user["id"])
        person = _person_row(user, created_at)
        person.update(person_id=scope.person(user_id), tags=["telegram_export_observation"])
        person_rows.append(person)
        observations.setdefault(("user", user_id), []).append(_display_name(user))
        for username in _usernames(user):
            observations.setdefault(("username", username), []).append(f"@{username}")
            observations[("user", user_id)].append(f"@{username}")
        phone = str(user.get("phone_number") or "").strip()
        if phone:
            observations.setdefault(("phone", phone), []).append(phone)
            observations[("user", user_id)].append(phone)
    chat = _chat(payload)
    for observed_chat in (chat, payload.get("sender_chat") or {}):
        if observed_chat.get("id") is not None:
            observations.setdefault(("chat", str(observed_chat["id"])), []).append(
                str(observed_chat.get("title") or "").strip()
            )
    message = _message(payload)
    sender = message.get("sender_id") or {}
    for kind, field in (("user", "user_id"), ("chat", "chat_id")):
        if sender.get(field) is not None:
            observations.setdefault((kind, str(sender[field])), [])
    for user_id in chat.get("participant_user_ids") or []:
        observations.setdefault(("user", str(user_id)), [])
    for (kind, value), labels in observations.items():
        identity_id = scope.identity(kind, value)
        # Export keys and labels do not prove ownership, even when numeric.
        identity_rows.append(
            _identity_row(
                identity_id=identity_id,
                person_id=None,
                kind=f"export_{kind}",
                value=scope.value(kind, value),
                label=f"Telegram export {kind} observation",
                created_at=created_at,
            )
        )
        for label in sorted(set(labels) - {""}):
            alias_rows.append(
                _identity_alias_row(identity_id, label, "telegram_export_observation", created_at)
            )
    return person_rows, identity_rows, alias_rows


def sync_telegram_entities(paths: RecallPaths, *, date: str) -> TelegramEntitySyncResult:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, updates_path = raw_capture_paths(paths, date)
    if not updates_path.exists():
        raise FileNotFoundError(f"Missing Telegram raw capture for {date} in {raw_dir}")

    rows = _load_raw_updates(updates_path)
    scopes = [export_scope(row, f"{updates_path}:{row['_line_number']}") for row in rows]
    created_at = _created_at(rows, date)
    person_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    identity_alias_rows: list[dict[str, Any]] = []
    person_alias_rows: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []

    person_seen: set[str] = set()
    identity_seen: set[tuple[str, str, str]] = set()
    identity_alias_seen: set[tuple[str, str, str]] = set()
    person_alias_seen: set[tuple[str, str, str]] = set()
    resolution_seen: set[tuple[str, str, str]] = set()

    users_by_id: dict[str, dict[str, Any]] = {}
    chats: list[dict[str, Any]] = []

    export_persons: list[dict[str, Any]] = []
    export_identities: list[dict[str, Any]] = []
    export_aliases: list[dict[str, Any]] = []
    for row, scope in zip(rows, scopes, strict=True):
        payload = _payload(row)
        if scope:
            observed_persons, observed_identities, observed_aliases = _export_observations(
                scope, payload, created_at
            )
            export_persons.extend(observed_persons)
            export_identities.extend(observed_identities)
            export_aliases.extend(observed_aliases)
            continue
        chats.append(_chat(payload))
        for user in payload.get("users") or []:
            if isinstance(user, dict) and user.get("id") is not None:
                users_by_id[str(user["id"])] = user

    # Count each observed key once and keep its last label in this capture day.
    export_persons = list({row["person_id"]: row for row in export_persons}.values())
    export_identities = list({row["identity_id"]: row for row in export_identities}.values())
    export_aliases = list({row["identity_alias_id"]: row for row in export_aliases}.values())

    for user in users_by_id.values():
        user_id = str(user["id"])
        person_id = _person_id(user_id)
        if person_id not in person_seen:
            person_rows.append(_person_row(user, created_at))
            person_seen.add(person_id)

        user_identity = ("user_id", user_id, _user_identity_id(user_id))
        if user_identity not in identity_seen:
            identity_rows.append(
                _identity_row(
                    identity_id=user_identity[2],
                    person_id=person_id,
                    kind="user_id",
                    value=user_id,
                    label="Telegram user ID",
                    created_at=created_at,
                    is_primary=True,
                )
            )
            identity_seen.add(user_identity)

        resolution_key = (user_identity[2], person_id, "telegram_user_id")
        if resolution_key not in resolution_seen:
            resolution_rows.append(
                _resolution_row(
                    user_identity[2],
                    person_id,
                    created_at,
                    method="telegram_user_id",
                    confidence="high",
                    evidence=[f"Observed exact Telegram user id {user_id}"],
                )
            )
            resolution_seen.add(resolution_key)

        first = str(user.get("first_name") or "").strip()
        last = str(user.get("last_name") or "").strip()
        full_name = " ".join(part for part in (first, last) if part).strip()
        person_aliases: list[tuple[str, str]] = []
        identity_aliases: list[tuple[str, str, str]] = []
        if full_name:
            person_aliases.append((full_name, "telegram_display_name"))
            identity_aliases.append((user_identity[2], full_name, "telegram_display_name"))
        elif first:
            person_aliases.append((first, "telegram_display_name"))
            identity_aliases.append((user_identity[2], first, "telegram_display_name"))

        for username in _usernames(user):
            username_identity = ("username", username, _username_identity_id(username))
            if username_identity not in identity_seen:
                identity_rows.append(
                    _identity_row(
                        identity_id=username_identity[2],
                        person_id=person_id,
                        kind="username",
                        value=username,
                        label="Telegram username",
                        created_at=created_at,
                    )
                )
                identity_seen.add(username_identity)
            resolution_key = (username_identity[2], person_id, "telegram_username")
            if resolution_key not in resolution_seen:
                resolution_rows.append(
                    _resolution_row(
                        username_identity[2],
                        person_id,
                        created_at,
                        method="telegram_username",
                        confidence="high",
                        evidence=[f"Observed active Telegram username @{username}"],
                    )
                )
                resolution_seen.add(resolution_key)

            handle = f"@{username}"
            person_aliases.append((handle, "telegram_username"))
            identity_aliases.append((username_identity[2], handle, "telegram_username"))
            identity_aliases.append((user_identity[2], handle, "telegram_username"))

        phone_number = str(user.get("phone_number") or "").strip()
        if phone_number:
            phone_identity = ("phone_number", phone_number, _phone_identity_id(phone_number))
            if phone_identity not in identity_seen:
                identity_rows.append(
                    _identity_row(
                        identity_id=phone_identity[2],
                        person_id=person_id,
                        kind="phone_number",
                        value=phone_number,
                        label="Telegram phone number",
                        created_at=created_at,
                    )
                )
                identity_seen.add(phone_identity)
            resolution_key = (phone_identity[2], person_id, "telegram_phone_number")
            if resolution_key not in resolution_seen:
                resolution_rows.append(
                    _resolution_row(
                        phone_identity[2],
                        person_id,
                        created_at,
                        method="telegram_phone_number",
                        confidence="high",
                        evidence=[f"Observed Telegram phone number {phone_number}"],
                    )
                )
                resolution_seen.add(resolution_key)

            person_aliases.append((phone_number, "telegram_phone_number"))
            identity_aliases.append((phone_identity[2], phone_number, "telegram_phone_number"))
            identity_aliases.append((user_identity[2], phone_number, "telegram_phone_number"))

        for value, source in person_aliases:
            key = (person_id, value, source)
            if key not in person_alias_seen:
                person_alias_rows.append(_person_alias_row(person_id, value, source, created_at))
                person_alias_seen.add(key)

        for identity_id, value, source in identity_aliases:
            key = (identity_id, value, source)
            if key not in identity_alias_seen:
                identity_alias_rows.append(
                    _identity_alias_row(identity_id, value, source, created_at)
                )
                identity_alias_seen.add(key)

    for chat in chats:
        if chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        chat_type_value = chat.get("type")
        chat_type: dict[str, Any] = chat_type_value if isinstance(chat_type_value, dict) else {}
        chat_type_name = str(chat_type.get("@type") or "")
        linked_person_id: str | None = None
        linked_user_id = chat_type.get("user_id")
        if chat_type_name in {"chatTypePrivate", "chatTypeSecret"} and linked_user_id is not None:
            linked_person_id = _person_id(linked_user_id)

        chat_identity = ("chat_id", chat_id, _chat_identity_id(chat_id))
        if chat_identity not in identity_seen:
            identity_rows.append(
                _identity_row(
                    identity_id=chat_identity[2],
                    person_id=linked_person_id,
                    kind="chat_id",
                    value=chat_id,
                    label="Telegram chat ID",
                    created_at=created_at,
                )
            )
            identity_seen.add(chat_identity)

        if linked_person_id is not None:
            resolution_key = (chat_identity[2], linked_person_id, "telegram_private_chat")
            if resolution_key not in resolution_seen:
                resolution_rows.append(
                    _resolution_row(
                        chat_identity[2],
                        linked_person_id,
                        created_at,
                        method="telegram_private_chat",
                        confidence="high",
                        evidence=[f"Telegram private chat maps to user id {linked_user_id}"],
                    )
                )
                resolution_seen.add(resolution_key)

        title = str(chat.get("title") or "").strip()
        if title:
            key = (chat_identity[2], title, "telegram_chat_title")
            if key not in identity_alias_seen:
                identity_alias_rows.append(
                    _identity_alias_row(chat_identity[2], title, "telegram_chat_title", created_at)
                )
                identity_alias_seen.add(key)

    persons_synced = upsert_persons(paths, person_rows) + upsert_persons(
        paths, export_persons, preserve_existing=True
    )
    identities_synced = upsert_identities(paths, identity_rows) + upsert_identities(
        paths, export_identities, preserve_existing=True
    )
    person_aliases_synced = upsert_aliases(paths, person_alias_rows)
    aliases_synced = upsert_identity_aliases(paths, identity_alias_rows + export_aliases)
    resolutions_synced = upsert_resolutions(paths, resolution_rows)
    return TelegramEntitySyncResult(
        persons_synced=persons_synced,
        identities_synced=identities_synced,
        aliases_synced=aliases_synced,
        person_aliases_synced=person_aliases_synced,
        resolutions_synced=resolutions_synced,
    )
