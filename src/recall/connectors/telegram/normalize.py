from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.connectors.telegram.capture import parse_date, raw_capture_paths
from recall.normalize.artifacts import NormalizedArtifact, RemoteLocator
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_artifact_metadata, write_normalized_events
from recall.storage.paths import RecallPaths

URL_PATTERN = re.compile(r'https?://[^\s)>"]+')


def _load_raw_updates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = index
            rows.append(row)
    return rows


def _to_iso(value: int | float | str | None, fallback: str) -> str:
    if value is None:
        return fallback
    timestamp = int(value)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _source_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return URL_PATTERN.findall(text)


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if isinstance(message, dict):
        return message
    return payload


def _user_map(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    users = payload.get("users") or []
    result: dict[int, dict[str, Any]] = {}
    for user in users:
        if isinstance(user, dict) and user.get("id") is not None:
            result[int(user["id"])] = user
    return result


def _chat(payload: dict[str, Any]) -> dict[str, Any]:
    chat = payload.get("chat")
    if isinstance(chat, dict):
        return chat
    return {}


def _user_identity_id(user_id: int | str) -> str:
    return f"ident_telegram_user_{user_id}"


def _chat_identity_id(chat_id: int | str) -> str:
    return f"ident_telegram_chat_{chat_id}"


def _sender_identity_id(message: dict[str, Any]) -> str | None:
    sender = message.get("sender_id") or {}
    sender_type = sender.get("@type")
    if sender_type == "messageSenderUser" and sender.get("user_id") is not None:
        return _user_identity_id(sender["user_id"])
    if sender_type == "messageSenderChat" and sender.get("chat_id") is not None:
        return _chat_identity_id(sender["chat_id"])
    return None


def _participant_identity_ids(payload: dict[str, Any], message: dict[str, Any]) -> list[str]:
    chat = _chat(payload)
    identities: set[str] = set()
    for user_id in chat.get("participant_user_ids") or []:
        identities.add(_user_identity_id(user_id))

    chat_type_value = chat.get("type")
    chat_type: dict[str, Any] = chat_type_value if isinstance(chat_type_value, dict) else {}
    if (
        chat_type.get("@type") in {"chatTypePrivate", "chatTypeSecret"}
        and chat_type.get("user_id") is not None
    ):
        identities.add(_user_identity_id(chat_type["user_id"]))

    sender_identity_id = _sender_identity_id(message)
    if sender_identity_id is not None:
        identities.add(sender_identity_id)

    return sorted(identities)


def _display_name(user: dict[str, Any]) -> str | None:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part).strip()
    if full:
        return full
    usernames = user.get("usernames") or []
    if usernames:
        return f"@{usernames[0]}"
    return None


def _conversation_label(payload: dict[str, Any], message: dict[str, Any]) -> str:
    chat = _chat(payload)
    title = str(chat.get("title") or "").strip()
    if title:
        return title

    chat_type_value = chat.get("type")
    chat_type: dict[str, Any] = chat_type_value if isinstance(chat_type_value, dict) else {}
    chat_type_name = str(chat_type.get("@type") or "")
    if (
        chat_type_name in {"chatTypePrivate", "chatTypeSecret"}
        and chat_type.get("user_id") is not None
    ):
        user = _user_map(payload).get(int(chat_type["user_id"]))
        if user is not None:
            display_name = _display_name(user)
            if display_name:
                return display_name

    sender_value = message.get("sender_id")
    sender: dict[str, Any] = sender_value if isinstance(sender_value, dict) else {}
    if chat_type_name == "chatTypePrivate" and chat_type.get("user_id") == sender.get("user_id"):
        user = _user_map(payload).get(int(chat_type["user_id"]))
        if user is not None:
            display_name = _display_name(user)
            if display_name:
                return display_name

    if sender.get("@type") == "messageSenderUser" and sender.get("user_id") is not None:
        user = _user_map(payload).get(int(sender["user_id"]))
        if user is not None:
            display_name = _display_name(user)
            if display_name:
                return display_name

    chat_id = message.get("chat_id")
    return f"chat:{chat_id}" if chat_id is not None else "unknown-chat"


def _extract_text_and_tags(content: dict[str, Any]) -> tuple[str, list[str]]:
    content_type = str(content.get("@type") or "")
    tags = ["message", "telegram"]
    if content_type == "messageText":
        text = content.get("text", {}).get("text")
        return str(text or ""), tags + ["text"]
    if content_type == "messagePhoto":
        caption = content.get("caption", {}).get("text")
        return str(caption or ""), tags + ["photo"]
    if content_type == "messageDocument":
        caption = content.get("caption", {}).get("text")
        return str(caption or ""), tags + ["document"]
    if content_type == "messageVoiceNote":
        caption = content.get("caption", {}).get("text")
        return str(caption or ""), tags + ["voice_note"]
    return str(content.get("caption", {}).get("text") or ""), tags


def _thread_id(message: dict[str, Any]) -> str | None:
    reply_to_message_id = message.get("reply_to_message_id")
    if reply_to_message_id is not None:
        return f"reply:{reply_to_message_id}"

    reply_to = message.get("reply_to")
    if isinstance(reply_to, dict):
        origin = reply_to.get("origin") if isinstance(reply_to.get("origin"), dict) else reply_to
        message_id = origin.get("message_id") if isinstance(origin, dict) else None
        if message_id is not None:
            return f"reply:{message_id}"

    media_album_id = message.get("media_album_id")
    if media_album_id is not None:
        return f"album:{media_album_id}"
    return None


def _message_tags(message: dict[str, Any], tags: list[str]) -> list[str]:
    result = list(tags)
    if _thread_id(message) and str(_thread_id(message)).startswith("reply:"):
        result.append("reply")
    if message.get("forward_info") is not None:
        result.append("forwarded")
    if message.get("media_album_id") is not None:
        result.append("album")
    return result


def _artifact_id(account: str, message_id: int | str, file_id: str) -> str:
    payload = f"telegram:{account}:artifact:{message_id}:{file_id}".encode("utf-8")
    return f"artifact_{hashlib.sha256(payload).hexdigest()[:20]}"


def _event_id(account: str, chat_id: int | str, message_id: int | str) -> str:
    payload = f"telegram:{account}:{chat_id}:{message_id}".encode("utf-8")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def _best_photo_file(photo: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    sizes = photo.get("sizes")
    if not isinstance(sizes, list):
        return None

    candidates: list[tuple[tuple[int, int, int], dict[str, Any], dict[str, Any]]] = []
    for size in sizes:
        if not isinstance(size, dict):
            continue
        file_value = size.get("photo")
        file_object: dict[str, Any] = file_value if isinstance(file_value, dict) else {}
        if not file_object:
            continue
        expected_size = int(file_object.get("expected_size") or 0)
        width = int(size.get("width") or 0)
        height = int(size.get("height") or 0)
        candidates.append(((expected_size, width * height, max(width, height)), file_object, size))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, file_object, size = candidates[-1]
    return file_object, size


def _file_details(content: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    content_type = str(content.get("@type") or "")
    if content_type == "messagePhoto":
        photo = content.get("photo") or {}
        if not isinstance(photo, dict):
            return None
        best = _best_photo_file(photo)
        if best is not None:
            file_object, size = best
            metadata = {
                "filename": None,
                "mime_type": None,
                "size_bytes": file_object.get("expected_size"),
                "width": size.get("width"),
                "height": size.get("height"),
            }
            return ("photo", file_object, metadata)
        return ("photo", photo, {}) if isinstance(photo, dict) else None
    if content_type == "messageDocument":
        document = content.get("document") or {}
        if not isinstance(document, dict):
            return None
        nested = document.get("document")
        if isinstance(nested, dict):
            metadata = {
                "filename": document.get("file_name"),
                "mime_type": document.get("mime_type"),
                "size_bytes": nested.get("expected_size") or document.get("size"),
            }
            return ("document", nested, metadata)
        return ("document", document, {})
    if content_type == "messageVoiceNote":
        voice_note = content.get("voice_note") or {}
        if not isinstance(voice_note, dict):
            return None
        nested = voice_note.get("voice")
        if isinstance(nested, dict):
            metadata = {
                "filename": voice_note.get("file_name"),
                "mime_type": voice_note.get("mime_type"),
                "size_bytes": nested.get("expected_size") or voice_note.get("size"),
            }
            return ("voice_note", nested, metadata)
        return ("voice_note", voice_note, {})
    return None


def _artifact_records(
    *,
    account: str,
    message: dict[str, Any],
    content: dict[str, Any],
    event_id: str,
    timestamp: str,
    raw_path: str,
    line_number: int,
) -> list[NormalizedArtifact]:
    file_details = _file_details(content)
    if file_details is None:
        return []

    kind, file_object, metadata = file_details
    file_id = str(
        file_object.get("id") or file_object.get("remote", {}).get("id") or f"line-{line_number}"
    )
    raw_ref = RawReference(
        source="telegram",
        path=raw_path,
        locator={
            "line": line_number,
            "message_id": message.get("id"),
            "file_id": file_id,
        },
    )
    locators: list[RemoteLocator] = []
    local_path = file_object.get("local", {}).get("path")
    remote_id = file_object.get("remote", {}).get("id")
    remote_unique_id = file_object.get("remote", {}).get("unique_id")
    if isinstance(local_path, str) and local_path:
        locators.append(RemoteLocator(kind="local_path", value=local_path))
    if isinstance(remote_id, str) and remote_id:
        locators.append(RemoteLocator(kind="remote_id", value=remote_id))
    if isinstance(remote_unique_id, str) and remote_unique_id:
        locators.append(RemoteLocator(kind="remote_unique_id", value=remote_unique_id))

    return [
        NormalizedArtifact(
            artifact_id=_artifact_id(
                account, str(message.get("id") or f"line-{line_number}"), file_id
            ),
            source="telegram",
            account=account,
            kind=kind,
            source_object_id=file_id,
            event_ids=[event_id],
            remote_locators=locators,
            local_path=None,
            mime_type=metadata.get("mime_type") or file_object.get("mime_type"),
            filename=metadata.get("filename") or file_object.get("file_name"),
            size_bytes=(
                int(metadata["size_bytes"])
                if metadata.get("size_bytes") is not None
                else int(file_object["size"])
                if file_object.get("size") is not None
                else None
            ),
            checksums={},
            download_status="not_requested",
            observed_at=timestamp,
            raw_ref=raw_ref,
        )
    ]


def normalize_telegram_day(paths: RecallPaths, *, date: str) -> tuple[Path, Path]:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, updates_path = raw_capture_paths(paths, date)
    if not updates_path.exists():
        raise FileNotFoundError(f"Missing Telegram raw capture for {date} in {raw_dir}")

    rows = _load_raw_updates(updates_path)
    events: list[NormalizedEvent] = []
    artifacts: list[NormalizedArtifact] = []

    for row in rows:
        payload_value = row.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        message = _message(payload)
        content_value = message.get("content")
        content: dict[str, Any] = content_value if isinstance(content_value, dict) else {}
        chat_id = message.get("chat_id")
        message_id = message.get("id")
        if chat_id is None or message_id is None:
            continue

        timestamp = _to_iso(message.get("date"), str(row.get("received_at") or f"{date}T00:00:00Z"))
        account = str(row.get("account") or "personal")
        event_id = _event_id(account, chat_id, message_id)
        raw_path = paths.relative_to_root(updates_path)
        text, tags = _extract_text_and_tags(content)
        event_artifacts = _artifact_records(
            account=account,
            message=message,
            content=content,
            event_id=event_id,
            timestamp=timestamp,
            raw_path=raw_path,
            line_number=row["_line_number"],
        )
        artifacts.extend(event_artifacts)
        events.append(
            NormalizedEvent(
                event_id=event_id,
                source="telegram",
                account=account,
                timestamp=timestamp,
                date=date,
                kind="message",
                conversation_id=str(chat_id),
                conversation_label=_conversation_label(payload, message),
                thread_id=_thread_id(message),
                sender_identity_id=_sender_identity_id(message),
                participant_identity_ids=_participant_identity_ids(payload, message),
                text=text,
                source_urls=_source_urls(text),
                artifact_ids=[artifact.artifact_id for artifact in event_artifacts],
                raw_ref=RawReference(
                    source="telegram",
                    path=raw_path,
                    locator={
                        "line": row["_line_number"],
                        "message_id": message_id,
                        "chat_id": chat_id,
                    },
                ),
                raw_fragment=None,
                tags=_message_tags(message, tags),
            )
        )

    artifact_path = write_artifact_metadata(
        paths, source="telegram", date=date, artifacts=artifacts
    )
    event_path = write_normalized_events(paths, date, events, merge_existing=True)
    return event_path, artifact_path
