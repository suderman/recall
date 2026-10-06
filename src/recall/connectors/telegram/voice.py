"""Read-only candidates from verified native Telegram plain messages."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from recall.connectors.telegram.normalize import _event_id, _to_iso
from recall.storage.references import raw_reference

QUOTATION = re.compile(
    r'(?im)["“”«»]|^[ \t]*>|^On [^\n]{0,300}(?:\n[^\n]{0,300}){0,4}?wrote:[ \t]*\r?$'
    r"|^[-_]{2,}[^\n]*(?:forwarded message|original message)[^\n]*$"
    r"|^From:[^\n]*\n(?:Sent|Date):[^\n]*$|^_{5,}[ \t]*\r?$"
    r"|^(?:Begin forwarded message|Forwarded message):[ \t]*\r?$"
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate raw JSON key")
        result[key] = value
    return result


def candidate(event: dict[str, Any], normalized: Path) -> dict[str, Any]:
    empty = {"passage": None, "exclusions": []}

    def excluded(reason: str) -> dict[str, Any]:
        return {**empty, "reason": reason}

    raw = event.get("raw_ref")
    if (
        not isinstance(raw, dict)
        or raw.get("source") != "telegram"
        or not isinstance(raw.get("path"), str)
        or not isinstance(raw.get("locator"), dict)
    ):
        return excluded("raw_unverifiable")
    locator = raw["locator"]
    line = locator.get("line")
    if type(line) is not int or line < 1:
        return excluded("raw_unverifiable")
    try:
        path, error = raw_reference(event, normalized)
        if path is None or error:
            return excluded("raw_unverifiable")
        data = path.read_bytes()
        raw_line = data.splitlines()[line - 1]
        row = json.loads(raw_line, object_pairs_hook=_unique_object)
        if not isinstance(row, dict) or row.get("source") != "telegram":
            return excluded("raw_unverifiable")
        if (
            row.get("capture_mode")
            not in {"tdlib-once", "tdlib-run", "tdlib-daemon", "pending-offline"}
            or row.get("import_id") is not None
        ):
            return excluded("unsupported_capture")
        payload = row.get("payload")
        if (
            row.get("update_type") != "updateNewMessage"
            or not isinstance(payload, dict)
            or payload.get("@type") != "updateNewMessage"
        ):
            return excluded("unsupported_capture")
        if row.get("account") != event.get("account"):
            return excluded("raw_account_mismatch")
        message = payload.get("message")
        if not isinstance(message, dict):
            return excluded("raw_unverifiable")
        message_id, chat_id, timestamp = (
            message.get("id"),
            message.get("chat_id"),
            message.get("date"),
        )
        if type(message_id) is not int or type(chat_id) is not int or type(timestamp) is not int:
            return excluded("raw_unverifiable")
        if (
            type(locator.get("message_id")) is not int
            or type(locator.get("chat_id")) is not int
            or message_id != locator["message_id"]
            or chat_id != locator["chat_id"]
            or event.get("conversation_id") != str(chat_id)
        ):
            return excluded("raw_message_id_mismatch")
        if event["event_id"] != _event_id(row["account"], chat_id, message_id):
            return excluded("raw_event_id_mismatch")
        if event["timestamp"] != _to_iso(timestamp, ""):
            return excluded("raw_timestamp_mismatch")
        sender = message.get("sender_id")
        if not isinstance(sender, dict):
            return excluded("raw_unverifiable")
        if sender.get("@type") != "messageSenderUser":
            return excluded("unsupported_sender")
        user_id = sender.get("user_id")
        if type(user_id) is not int or user_id < 1:
            return excluded("raw_sender_unverifiable")
        if event.get("sender_identity_id") != f"ident_telegram_user_{user_id}":
            return excluded("raw_sender_mismatch")
        users = payload.get("users")
        if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
            return excluded("raw_sender_unverifiable")
        matches = [user for user in users if type(user.get("id")) is int and user["id"] == user_id]
        if len(matches) != 1 or not isinstance(matches[0].get("type"), dict):
            return excluded("raw_sender_unverifiable")
        if matches[0]["type"].get("@type") == "userTypeBot":
            return excluded("bot_origin")
        if matches[0]["type"].get("@type") != "userTypeRegular":
            return excluded("raw_sender_unverifiable")
        if message.get("forward_info") is not None or "forwarded" in (event.get("tags") or []):
            return excluded("forwarded")
        if any(
            message.get(key) not in (None, 0)
            for key in (
                "via_bot_user_id",
                "via_business_bot_user_id",
                "sender_business_bot_user_id",
            )
        ):
            return excluded("bot_origin")
        content = message.get("content")
        if not isinstance(content, dict) or content.get("@type") != "messageText":
            return excluded("unsupported_content")
        formatted = content.get("text")
        if not isinstance(formatted, dict) or not isinstance(formatted.get("text"), str):
            return excluded("text_format_unverifiable")
        text, entities = formatted["text"], formatted.get("entities")
        if text != event.get("text"):
            return excluded("raw_text_mismatch")
        if not isinstance(entities, list):
            return excluded("text_format_unverifiable")
        # Plain text only for this slice. Do not reinterpret TDLib's UTF-16 entity offsets.
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("type"), dict):
                return excluded("text_format_unverifiable")
            kind = entity["type"].get("@type")
            if kind in {"textEntityTypeBlockQuote", "textEntityTypeExpandableBlockQuote"}:
                return excluded("ambiguous_quotation")
            if kind in {"textEntityTypeCode", "textEntityTypePre", "textEntityTypePreCode"}:
                return excluded("non_prose")
            return excluded("unsupported_formatting")
        if not text.strip():
            return excluded("no_original_passage")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|`|~~~", text):
            return excluded("non_prose")
        if QUOTATION.search(text):
            return excluded("ambiguous_quotation")
        if re.search(r"(?im)^[^\r\n]*<[^<>\r\n]+@[^<>\r\n]+>:[ \t]*\r?$|^\[image\]", text):
            return excluded("ambiguous_attribution")
        if path.read_bytes() != data:
            return excluded("raw_changed_during_read")
        body_hash = hashlib.sha256(text.encode()).hexdigest()
        return {
            **empty,
            "passage": {"text": text, "start": 0, "end": len(text), "sha256": body_hash},
            "raw_path": str(path),
            "raw_sha256": hashlib.sha256(data).hexdigest(),
            "raw_line": line,
            "raw_record_sha256": hashlib.sha256(raw_line).hexdigest(),
            "raw_sender": user_id,
            "body_sha256": body_hash,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return excluded("raw_unverifiable")
