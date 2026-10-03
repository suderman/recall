from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall.connectors.telegram.capture import append_telegram_envelope, local_date_for_timestamp
from recall.storage.paths import RecallPaths

ID_SUFFIX_PATTERN = re.compile(r"(\d+)$")


@dataclass(frozen=True, slots=True)
class TelegramImportResult:
    import_id: str
    import_dir: Path
    dates_written: list[str]
    messages_imported: int
    messages_skipped: int = 0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid Telegram export JSON at {path}") from exc


def _coerce_numeric_id(prefix: str, value: Any) -> int:
    text = str(value or "").strip()
    match = ID_SUFFIX_PATTERN.search(text)
    if match:
        return int(match.group(1))
    digest = hashlib.sha256(f"{prefix}:{text}".encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def _import_id(source_path: Path, result: dict[str, Any]) -> str:
    export_name = str(result.get("name") or source_path.stem or source_path.name).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", export_name).strip("_") or "telegram"
    digest = hashlib.sha256(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"telegram_export_{normalized}_{digest}"


def _text_from_message(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts: list[str] = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


def _content_from_message(message: dict[str, Any], export_root: Path) -> dict[str, Any]:
    text = _text_from_message(message)
    file_path_value = message.get("photo") or message.get("file")
    mime_type = str(message.get("mime_type") or "").strip() or None
    if file_path_value:
        relative_path = Path(str(file_path_value))
        local_path = export_root / relative_path
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or not local_path.resolve().is_relative_to(export_root.resolve())
        ):
            raise ValueError(
                f"Telegram export media must stay inside the retained root: {relative_path}"
            )
        component = export_root
        for part in relative_path.parts:
            component = component / part
            if component.is_symlink() and component.readlink().is_absolute():
                raise ValueError(f"Telegram export media has an absolute symlink: {relative_path}")
        if local_path.exists() and not local_path.is_file():
            raise ValueError(f"Telegram export media is not a regular file: {relative_path}")
        file_object = {
            "id": _coerce_numeric_id("telegram-export-file", relative_path.as_posix()),
            "local": {
                "path": str(local_path),
                "is_downloading_completed": local_path.exists(),
            },
            "remote": {},
            "expected_size": local_path.stat().st_size if local_path.exists() else None,
            "mime_type": mime_type,
            "file_name": relative_path.name,
        }
        if message.get("photo"):
            return {
                "@type": "messagePhoto",
                "caption": {"text": text},
                "photo": {
                    "sizes": [
                        {
                            "width": 0,
                            "height": 0,
                            "photo": file_object,
                        }
                    ]
                },
            }
        return {
            "@type": "messageDocument",
            "caption": {"text": text},
            "document": {
                "file_name": relative_path.name,
                "mime_type": mime_type,
                "document": file_object,
            },
        }

    return {
        "@type": "messageText",
        "text": {"text": text},
    }


def _user_record(message: dict[str, Any]) -> dict[str, Any] | None:
    from_name = str(message.get("from") or "").strip()
    from_id = message.get("from_id")
    if isinstance(from_id, str) and from_id.startswith(("channel", "chat")):
        return None
    if not from_name and from_id is None:
        return None
    user_id = _coerce_numeric_id("telegram-export-user", from_id or from_name)
    first, _, last = from_name.partition(" ")
    return {
        "id": user_id,
        "first_name": first or from_name or str(from_id),
        "last_name": last or "",
        "usernames": [],
    }


def _envelopes_from_chat(
    chat: dict[str, Any], export_root: Path, *, account: str, import_id: str
) -> list[tuple[str, dict[str, Any]]]:
    title = str(chat.get("name") or chat.get("title") or "").strip()
    chat_id = _coerce_numeric_id("telegram-export-chat", chat.get("id") or title)
    envelopes: list[tuple[str, dict[str, Any]]] = []
    for message in chat.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("type") or "") != "message":
            continue
        timestamp = str(message.get("date") or "").strip()
        if not timestamp:
            continue
        message_id = _coerce_numeric_id("telegram-export-message", message.get("id") or timestamp)
        user = _user_record(message)
        sender = {"@type": "messageSenderUser", "user_id": user["id"]} if user is not None else None
        from_id = message.get("from_id")
        if isinstance(from_id, str) and from_id.startswith(("channel", "chat")):
            sender = {
                "@type": "messageSenderChat",
                "chat_id": _coerce_numeric_id("telegram-export-chat", from_id),
            }
        payload = {
            "chat": {
                "id": chat_id,
                "title": title,
            },
            "message": {
                "id": message_id,
                "chat_id": chat_id,
                "date": int(message.get("date_unixtime") or 0) or None,
                "sender_id": sender,
                "content": _content_from_message(message, export_root),
            },
            "users": [user] if user is not None else [],
        }
        if sender and sender["@type"] == "messageSenderChat":
            payload["sender_chat"] = {
                "id": sender["chat_id"],
                "title": str(message.get("from") or ""),
            }
        # Retain explicit references, including targets absent from the bundle.
        for field in ("reply_to_message_id", "reply_to", "media_album_id", "message_thread_id"):
            if message.get(field) is not None:
                payload["message"][field] = message[field]
        envelope = {
            "received_at": timestamp,
            "source": "telegram",
            "account": account,
            "capture_mode": "import",
            "import_id": import_id,
            "update_type": "exportMessage",
            "payload": payload,
        }
        envelopes.append((local_date_for_timestamp(timestamp), envelope))
    return envelopes


def _prepare_export_dir(export_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    resolved = export_path.expanduser().resolve()
    if resolved.is_dir():
        return resolved, None
    if resolved.is_file() and resolved.suffix.lower() == ".zip":
        tempdir = tempfile.TemporaryDirectory(prefix="recall-telegram-export-")
        with zipfile.ZipFile(resolved) as archive:
            archive.extractall(tempdir.name)
        return Path(tempdir.name), tempdir
    raise FileNotFoundError(f"Telegram export must be a directory or .zip file: {resolved}")


def _copy_export(source_root: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(
            f"Telegram export import already exists at {destination}; "
            "remove it first or use a different export"
        )
    shutil.copytree(source_root, destination, symlinks=True)


def import_telegram_export(
    paths: RecallPaths,
    *,
    export_path: Path,
    account: str,
) -> TelegramImportResult:
    paths.ensure_directories()
    export_root, tempdir = _prepare_export_dir(export_path)
    try:
        result_path = export_root / "result.json"
        if not result_path.exists():
            matches = sorted(export_root.glob("**/result.json"))
            if len(matches) > 1:
                raise ValueError("Unsupported Telegram export: multiple result.json files")
            if matches:
                result_path = matches[0]
        if not result_path.exists():
            raise FileNotFoundError(f"Telegram export requires result.json in {export_root}")

        if not result_path.resolve().is_relative_to(export_root.resolve()):
            raise ValueError("Telegram result.json must stay inside the export root")
        result = _load_json(result_path)
        chats_value = result.get("chats") if isinstance(result, dict) else None
        chats = chats_value.get("list") if isinstance(chats_value, dict) else None
        if not isinstance(chats, list) or any(
            not isinstance(chat, dict) or not isinstance(chat.get("messages"), list)
            for chat in chats
        ):
            raise ValueError(
                "Unsupported Telegram export shape: expected chats.list with message lists; "
                "single-chat exports are not supported"
            )
        import_id = _import_id(export_path, result)
        import_dir = paths.raw_import_dir("telegram", import_id)
        # Validate the whole input before retaining it or appending any receipts.
        observed = sum(len(chat["messages"]) for chat in chats)
        supported = sum(
            len(
                _envelopes_from_chat(chat, result_path.parent, account=account, import_id=import_id)
            )
            for chat in chats
        )
        if observed and not supported:
            raise ValueError("No importable Telegram messages in nonempty export")
        _copy_export(result_path.parent, import_dir)

        dates_written: set[str] = set()
        messages_imported = 0
        export_base = import_dir
        for chat in chats:
            for date, envelope in _envelopes_from_chat(
                chat, export_base, account=account, import_id=import_id
            ):
                append_telegram_envelope(paths, date=date, envelope=envelope)
                dates_written.add(date)
                messages_imported += 1

        return TelegramImportResult(
            import_id=import_id,
            import_dir=import_dir,
            dates_written=sorted(dates_written),
            messages_imported=messages_imported,
            messages_skipped=observed - supported,
        )
    finally:
        if tempdir is not None:
            tempdir.cleanup()
