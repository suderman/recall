from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class BlueBubblesExportResult:
    output_dir: Path
    manifest_path: Path
    messages_path: Path
    message_count: int


def _safe_attachment_name(attachment_guid: str, filename: str | None) -> str:
    suffix = Path(filename or "attachment").name or "attachment"
    return f"{attachment_guid}--{suffix}"


def _parse_date(value: str) -> date_type:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _to_apple_nanoseconds(moment: datetime) -> int:
    delta = moment.astimezone(timezone.utc) - APPLE_EPOCH
    return int(delta.total_seconds() * 1_000_000_000)


def _from_apple_time(value: int | float | None) -> int | None:
    if value is None:
        return None
    numeric = int(value)
    if numeric == 0:
        return None
    seconds = numeric / 1_000_000_000
    timestamp = APPLE_EPOCH.timestamp() + seconds
    return int(timestamp * 1000)


def _date_bounds(from_date: str, to_date: str) -> tuple[int, int]:
    start_date = _parse_date(from_date)
    end_date = _parse_date(to_date)
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    return _to_apple_nanoseconds(start), _to_apple_nanoseconds(end)


def _fetch_messages(
    connection: sqlite3.Connection, from_date: str, to_date: str
) -> list[sqlite3.Row]:
    start_ns, end_ns = _date_bounds(from_date, to_date)
    return connection.execute(
        """
        select
          message.rowid as message_id,
          message.guid as message_guid,
          message.text as text,
          message.is_from_me as is_from_me,
          message.date as apple_date,
          handle.id as handle_id,
          chat.guid as chat_guid,
          chat.display_name as chat_display_name
        from message
        join chat_message_join on chat_message_join.message_id = message.rowid
        join chat on chat.rowid = chat_message_join.chat_id
        left join handle on handle.rowid = message.handle_id
        where message.date between ? and ?
        order by message.date asc, message.rowid asc
        """,
        (start_ns, end_ns),
    ).fetchall()


def _fetch_participants(connection: sqlite3.Connection, chat_guid: str) -> list[str]:
    rows = connection.execute(
        """
        select distinct handle.id
        from chat
        join chat_handle_join on chat_handle_join.chat_id = chat.rowid
        join handle on handle.rowid = chat_handle_join.handle_id
        where chat.guid = ? and handle.id is not null
        order by handle.id asc
        """,
        (chat_guid,),
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def _copy_attachment_file(source: Path, destination: Path) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    checksum = sha256()
    total = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while chunk := src.read(65536):
            dst.write(chunk)
            checksum.update(chunk)
            total += len(chunk)
    return checksum.hexdigest(), total


def _fetch_attachments(
    connection: sqlite3.Connection,
    message_id: int,
    *,
    include_attachment_bytes: bool,
    attachments_dir: Path,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        select
          attachment.guid as guid,
          attachment.filename as filename,
          attachment.mime_type as mime_type,
          attachment.transfer_name as transfer_name,
          attachment.total_bytes as total_bytes
        from attachment
        join message_attachment_join on message_attachment_join.attachment_id = attachment.rowid
        where message_attachment_join.message_id = ?
        order by attachment.rowid asc
        """,
        (message_id,),
    ).fetchall()

    attachments: list[dict[str, Any]] = []
    for row in rows:
        filename = row["filename"]
        attachment = {
            "guid": row["guid"],
            "filename": filename,
            "mimeType": row["mime_type"],
            "path": filename,
            "transferName": row["transfer_name"],
            "totalBytes": row["total_bytes"],
        }
        if include_attachment_bytes and filename:
            source_path = Path(str(filename)).expanduser()
            if source_path.exists() and source_path.is_file():
                relative_path = Path("attachments") / _safe_attachment_name(
                    str(row["guid"]), str(filename)
                )
                destination = attachments_dir / relative_path.name
                checksum, size = _copy_attachment_file(source_path, destination)
                attachment["bundleRelativePath"] = relative_path.as_posix()
                attachment["checksums"] = {"sha256": checksum}
                attachment["totalBytes"] = size
            else:
                attachment["exportError"] = f"Attachment file not found: {source_path}"

        attachments.append(attachment)
    return attachments


def export_bluebubbles_history(
    *,
    messages_db: Path,
    output_dir: Path,
    from_date: str,
    to_date: str,
    export_id: str | None = None,
    include_attachment_bytes: bool = False,
) -> BlueBubblesExportResult:
    database_path = messages_db.expanduser().resolve()
    if not database_path.exists():
        raise FileNotFoundError(f"Messages database not found: {database_path}")

    destination = output_dir.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Export output directory already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    attachments_dir = destination / "attachments"

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = _fetch_messages(connection, from_date, to_date)
        messages: list[dict[str, Any]] = []
        for row in rows:
            participants = _fetch_participants(connection, str(row["chat_guid"]))
            handle = str(row["handle_id"]) if row["handle_id"] else None
            if handle and handle not in participants:
                participants.append(handle)
                participants.sort()
            chat_label = str(row["chat_display_name"] or handle or row["chat_guid"])
            messages.append(
                {
                    "guid": row["message_guid"],
                    "dateCreated": _from_apple_time(row["apple_date"]),
                    "text": str(row["text"] or ""),
                    "isFromMe": bool(row["is_from_me"]),
                    "chatGuid": row["chat_guid"],
                    "chatDisplayName": chat_label,
                    "displayName": chat_label,
                    "handle": handle,
                    "participants": participants,
                    "attachments": _fetch_attachments(
                        connection,
                        int(row["message_id"]),
                        include_attachment_bytes=include_attachment_bytes,
                        attachments_dir=attachments_dir,
                    ),
                }
            )
    finally:
        connection.close()

    manifest = {
        "export_id": export_id or f"bluebubbles_{from_date}_{to_date}",
        "source": "bluebubbles",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "from": from_date,
        "to": to_date,
        "message_count": len(messages),
        "include_attachment_bytes": include_attachment_bytes,
    }
    manifest_path = destination / "manifest.json"
    messages_path = destination / "messages.jsonl"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with messages_path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, ensure_ascii=True, sort_keys=True))
            handle.write("\n")

    return BlueBubblesExportResult(
        output_dir=destination,
        manifest_path=manifest_path,
        messages_path=messages_path,
        message_count=len(messages),
    )
