"""Read-only candidates from verified native Telegram plain messages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from recall.connectors.telegram.normalize import _event_id, _to_iso
from recall.storage.references import raw_reference, resolve_reference

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


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha(value: Any) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Invalid history digest")


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _private(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("History proof must be an owner-only regular file")


def history_proof(row: dict[str, Any], raw: Path, data: bytes) -> dict[str, Any]:
    """Bind one snapshot to its authenticated context, fixed cohort and page locator."""
    recorded = Path(row["history_report"])
    if not recorded.is_absolute() or not recorded.name.endswith("-history.json"):
        raise ValueError("Invalid history report reference")
    watched: dict[Path, bytes] = {}

    def read(reference: Path) -> dict[str, Any]:
        path = resolve_reference(reference)
        _private(path)
        content = path.read_bytes()
        watched[path] = content
        try:
            value = json.loads(content, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid history proof JSON") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid history proof object")
        return value

    report = read(recorded)
    auth = read(recorded.parent / "authenticated-self.json")
    summary = read(recorded.parent / "capture-summary.json")
    message, chat, users = (row["payload"][key] for key in ("message", "chat", "users"))
    self_id, chat_id, cutoff = auth["self"]["id"], message["chat_id"], auth["cutoff"]
    if (
        not all(
            _positive(value) for value in (self_id, chat_id, cutoff, message["id"], message["date"])
        )
        or auth["account"] != row["account"]
        or summary["account"] != row["account"]
        or summary["self_id"] != self_id
        or type(summary["self_id"]) is not int
        or summary["cutoff"] != cutoff
        or type(summary["cutoff"]) is not int
        or auth["self"]["@type"] != "user"
        or auth["self"]["type"]["@type"] != "userTypeRegular"
        or type(summary["history_complete"]) is not bool
        or summary["history_complete"]
        or message["@type"] != "message"
        or message["sender_id"] != {"@type": "messageSenderUser", "user_id": self_id}
        or type(message["sender_id"]["user_id"]) is not int
        or message["date"] > cutoff
        or row["update_id"] is not None
        or report["chat_id"] != chat_id
        or type(report["chat_id"]) is not int
        or report["chat"] != chat
        or chat["@type"] != "chat"
        or type(chat["id"]) is not int
        or chat["id"] != chat_id
        or chat["type"] != {"@type": "chatTypePrivate", "user_id": chat_id}
        or type(chat["type"]["user_id"]) is not int
        or not isinstance(users, list)
        or len(users) != 2
        or any(
            user["@type"] != "user"
            or not _positive(user["id"])
            or user["type"]["@type"] != "userTypeRegular"
            for user in users
        )
        or {user["id"] for user in users} != {self_id, chat_id}
    ):
        raise ValueError("History authenticated user or private peer mismatch")
    for digest in (
        auth["self_response_sha256"],
        report["chat_response_sha256"],
        report["peer_response_sha256"],
    ):
        _sha(digest)
    if not isinstance(row["received_at"], str):
        raise ValueError("Invalid history receipt time")
    received = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
    if received.utcoffset() != timedelta(0) or received.timestamp() < cutoff:
        raise ValueError("History receipt predates capture")
    if (
        not isinstance(summary["raw"], str)
        or not Path(summary["raw"]).is_absolute()
        or resolve_reference(Path(summary["raw"])) != raw
        or summary["raw_sha256"] != hashlib.sha256(data).hexdigest()
        or data
        and not data.endswith(b"\n")
    ):
        raise ValueError("History raw snapshot mismatch")
    try:
        rows = [
            json.loads(line, object_pairs_hook=_unique_object)
            for line in data.splitlines()
            if line.strip()
        ]
    except (ValueError, UnicodeError):
        raise ValueError("Invalid history raw snapshot") from None
    selected = [item for item in rows if item["payload"]["message"]["chat_id"] == chat_id]
    count = report["selected_own"]
    counts = summary["counts"]
    if not isinstance(counts, dict) or any(
        not isinstance(value, dict)
        or type(value["own"]) is not int
        or value["own"] < 0
        or not _positive(value["pages"])
        or not isinstance(value["stop"], str)
        for value in counts.values()
    ):
        raise ValueError("Invalid history cohort counts")
    label = recorded.name.removesuffix("-history.json").casefold()
    matching = [value for name, value in counts.items() if name.casefold() == label]
    if (
        not _positive(count)
        or count != len(selected)
        or len({item["payload"]["message"]["id"] for item in selected}) != count
        or sum(value["own"] for value in counts.values()) != len(rows)
        or matching != [{"own": count, "pages": len(report["pages"]), "stop": report["stop"]}]
        or any(
            item["source"] != "telegram"
            or item["account"] != row["account"]
            or item["capture_mode"] != "tdlib-history"
            or item["update_type"] != "getChatHistoryMessage"
            or item["payload"]["@type"] != "getChatHistoryMessage"
            or item["history_report"] != str(recorded)
            or item["payload"]["message"]["sender_id"] != message["sender_id"]
            for item in selected
        )
    ):
        raise ValueError("History selected cohort mismatch")
    pages = report["pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError("Invalid bounded history pages")
    cursor, previous_date = 0, None
    for number, page in enumerate(pages):
        query, ids, dates = page["query"], page["message_ids"], page["dates"]
        if (
            query
            != {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": cursor,
                "offset": 0,
                "limit": 100,
                "only_local": False,
            }
            or any(
                type(query[key]) is not int
                for key in ("chat_id", "from_message_id", "offset", "limit")
            )
            or type(query["only_local"]) is not bool
            or query["only_local"]
            or not isinstance(ids, list)
            or not isinstance(dates, list)
            or len(ids) != len(dates)
            or len(ids) > 100
            or any(not _positive(value) for value in [*ids, *dates])
            or any(left <= right for left, right in zip(ids, ids[1:], strict=False))
            or any(left < right for left, right in zip(dates, dates[1:], strict=False))
            or cursor
            and (any(value > cursor for value in ids) or ids and ids[-1] >= cursor)
            or previous_date is not None
            and dates
            and dates[0] > previous_date
            or not ids
            and number != len(pages) - 1
        ):
            raise ValueError("History page or pagination mismatch")
        _sha(page["response_sha256"])
        if ids:
            cursor, previous_date = ids[-1], dates[-1]
    window, limit, stop = report["window"], report["requested_own"], report["stop"]
    if stop == "own_limit":
        if window is not None or not _positive(limit) or not count == limit <= 100:
            raise ValueError("History own-message cap mismatch")
    elif stop == "date_boundary":
        if (
            limit is not None
            or not isinstance(window, list)
            or len(window) != 2
            or not all(_positive(value) for value in window)
            or window[0] >= window[1]
            or not pages[-1]["dates"]
            or min(pages[-1]["dates"]) >= window[0]
        ):
            raise ValueError("History date boundary mismatch")
    else:
        raise ValueError("History cohort did not reach its bounded stop")
    loc = row["history"]
    page_number, index = loc["page"], loc["message_index"]
    if (
        type(page_number) is not int
        or type(index) is not int
        or not 0 <= page_number < len(pages)
        or index < 0
    ):
        raise ValueError("Invalid history page locator")
    page = pages[page_number]
    if (
        page["message_ids"][index] != message["id"]
        or page["dates"][index] != message["date"]
        or loc["message_sha256"] != _digest(message)
        or window is not None
        and not window[0] <= message["date"] < window[1]
    ):
        raise ValueError("History message/page hash mismatch")
    for path, content in watched.items():
        _private(path)
        if path.read_bytes() != content:
            raise ValueError("History proof changed during inspection")
    return {
        "history_proof": {
            "version": 1,
            "report_path": str(recorded),
            "authenticated_self_id": self_id,
            "cutoff": cutoff,
            "page": page_number,
            "message_index": index,
            "message_sha256": loc["message_sha256"],
            "response_sha256": page["response_sha256"],
            "stop": stop,
            "selected_own": count,
        },
        "proof_inputs": [
            {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(watched.items())
        ],
    }


def candidate(event: dict[str, Any], normalized: Path) -> dict[str, Any]:
    empty = {"passage": None, "exclusions": []}
    proof: dict[str, Any] = {}

    def excluded(reason: str) -> dict[str, Any]:
        return {**empty, **proof, "reason": reason}

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
        history = row.get("capture_mode") == "tdlib-history"
        if (
            row.get("capture_mode")
            not in {"tdlib-once", "tdlib-run", "tdlib-daemon", "pending-offline", "tdlib-history"}
            or row.get("import_id") is not None
        ):
            return excluded("unsupported_capture")
        payload = row.get("payload")
        expected_type = "getChatHistoryMessage" if history else "updateNewMessage"
        if (
            row.get("update_type") != expected_type
            or not isinstance(payload, dict)
            or payload.get("@type") != expected_type
        ):
            return excluded("raw_history_unverifiable" if history else "unsupported_capture")
        if history:
            try:
                proof = history_proof(row, path, data)
            except (
                OSError,
                UnicodeError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                OverflowError,
            ):
                return excluded("raw_history_unverifiable")
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
            **proof,
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
