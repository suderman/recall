from __future__ import annotations

from typing import Any


def message_chat(message: dict[str, Any]) -> dict[str, Any]:
    chat = message.get("chat")
    if isinstance(chat, dict):
        return chat
    chats = message.get("chats")
    if isinstance(chats, list) and chats and isinstance(chats[0], dict):
        return chats[0]
    return {}


def handle_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if not isinstance(value, dict):
        return None
    for key in ("address", "handle", "value", "identifier"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def participant_values(message: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for source in (message.get("participants"), message_chat(message).get("participants")):
        if not isinstance(source, list):
            continue
        for item in source:
            participant = handle_value(item)
            if participant:
                values.append(participant)
    handle = handle_value(message.get("handle"))
    if handle:
        values.append(handle)
    return sorted(set(values))
