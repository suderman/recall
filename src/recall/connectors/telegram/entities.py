from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall.connectors.telegram.capture import parse_date, raw_capture_paths
from recall.entities.storage import upsert_identities, upsert_identity_aliases
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class TelegramEntitySyncResult:
    identities_synced: int
    aliases_synced: int


def _load_raw_updates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _user_identity_id(user_id: int | str) -> str:
    return f"ident_telegram_user_{user_id}"


def _chat_identity_id(chat_id: int | str) -> str:
    return f"ident_telegram_chat_{chat_id}"


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


def _alias_row(identity_id: str, value: str, created_at: str, source: str) -> dict[str, Any]:
    return {
        "identity_alias_id": _identity_alias_id(identity_id, value, source),
        "identity_id": identity_id,
        "value": value,
        "source": source,
        "created_at": created_at,
    }


def sync_telegram_entities(paths: RecallPaths, *, date: str) -> TelegramEntitySyncResult:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, updates_path = raw_capture_paths(paths, date)
    if not updates_path.exists():
        raise FileNotFoundError(f"Missing Telegram raw capture for {date} in {raw_dir}")

    rows = _load_raw_updates(updates_path)
    created_at = _created_at(rows, date)
    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    identity_seen: set[str] = set()
    alias_seen: set[tuple[str, str, str]] = set()

    for row in rows:
        payload = _payload(row)
        for user in payload.get("users") or []:
            if not isinstance(user, dict) or user.get("id") is None:
                continue
            user_id = str(user["id"])
            identity_id = _user_identity_id(user_id)
            if identity_id not in identity_seen:
                identity_rows.append(
                    {
                        "identity_id": identity_id,
                        "person_id": None,
                        "source": "telegram",
                        "kind": "user_id",
                        "value": user_id,
                        "label": "Telegram user ID",
                        "is_primary": False,
                        "status": "active",
                        "valid_from": None,
                        "valid_to": None,
                        "created_at": created_at,
                    }
                )
                identity_seen.add(identity_id)

            aliases: list[tuple[str, str]] = []
            first = str(user.get("first_name") or "").strip()
            last = str(user.get("last_name") or "").strip()
            full_name = " ".join(part for part in (first, last) if part).strip()
            if full_name:
                aliases.append((full_name, "telegram_display_name"))
            elif first:
                aliases.append((first, "telegram_display_name"))
            for username in user.get("usernames") or []:
                text = f"@{str(username).lstrip('@').strip()}"
                if text != "@":
                    aliases.append((text, "telegram_username"))
            phone_number = str(user.get("phone_number") or "").strip()
            if phone_number:
                aliases.append((phone_number, "telegram_phone_number"))

            for value, source in aliases:
                key = (identity_id, value, source)
                if key in alias_seen:
                    continue
                alias_seen.add(key)
                alias_rows.append(_alias_row(identity_id, value, created_at, source))

        chat = _chat(payload)
        if chat.get("id") is not None:
            chat_identity_id = _chat_identity_id(chat["id"])
            if chat_identity_id not in identity_seen:
                identity_rows.append(
                    {
                        "identity_id": chat_identity_id,
                        "person_id": None,
                        "source": "telegram",
                        "kind": "chat_id",
                        "value": str(chat["id"]),
                        "label": "Telegram chat ID",
                        "is_primary": False,
                        "status": "active",
                        "valid_from": None,
                        "valid_to": None,
                        "created_at": created_at,
                    }
                )
                identity_seen.add(chat_identity_id)

            title = str(chat.get("title") or "").strip()
            if title:
                key = (chat_identity_id, title, "telegram_chat_title")
                if key not in alias_seen:
                    alias_seen.add(key)
                    alias_rows.append(
                        _alias_row(chat_identity_id, title, created_at, "telegram_chat_title")
                    )

        message = _message(payload)
        sender = message.get("sender_id") or {}
        if sender.get("@type") == "messageSenderChat" and sender.get("chat_id") is not None:
            chat_identity_id = _chat_identity_id(sender["chat_id"])
            if chat_identity_id not in identity_seen:
                identity_rows.append(
                    {
                        "identity_id": chat_identity_id,
                        "person_id": None,
                        "source": "telegram",
                        "kind": "chat_id",
                        "value": str(sender["chat_id"]),
                        "label": "Telegram chat ID",
                        "is_primary": False,
                        "status": "active",
                        "valid_from": None,
                        "valid_to": None,
                        "created_at": created_at,
                    }
                )
                identity_seen.add(chat_identity_id)

    identities_synced = upsert_identities(paths, identity_rows)
    aliases_synced = upsert_identity_aliases(paths, alias_rows)
    return TelegramEntitySyncResult(
        identities_synced=identities_synced,
        aliases_synced=aliases_synced,
    )
