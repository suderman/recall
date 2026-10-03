from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

from recall.connectors.bluebubbles.capture import (
    BLUEBUBBLES_CURSOR_KEY,
    _append_bluebubbles_envelope,
    advance_bluebubbles_cursor,
    bluebubbles_writer,
    local_date_for_timestamp,
)
from recall.connectors.bluebubbles.diagnostics import safe_error
from recall.connectors.bluebubbles.messages import handle_value, message_chat, participant_values
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor

DEFAULT_RECOVERY_HOURS = 72
DEFAULT_RECOVERY_PAGE_SIZE = 250
DEFAULT_RECOVERY_OVERLAP_SECONDS = 30


class BlueBubblesRecoveryHttpClient(Protocol):
    def post(self, url: str, *, json: dict[str, Any]) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BlueBubblesRecoveryResult:
    account: str
    cursor_before: str | None
    cursor_after: str | None
    window_start: str
    window_end: str
    recovered_messages: int
    skipped_existing: int
    dates_written: list[str]
    pages_fetched: int


def _parse_iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_message_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    if numeric > 1_000_000_000_000_000:
        numeric /= 1_000_000_000
    elif numeric > 10_000_000_000:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_attachments = message.get("attachments")
    if raw_attachments is None and isinstance(message.get("attachment"), list):
        raw_attachments = message.get("attachment")
    elif raw_attachments is None and isinstance(message.get("attachment"), dict):
        raw_attachments = [message.get("attachment")]

    attachments: list[dict[str, Any]] = []
    if not isinstance(raw_attachments, list):
        if isinstance(raw_attachments, dict):
            raw_attachments = [raw_attachments]
        else:
            return attachments

    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        attachments.append(
            {
                "guid": item.get("guid") or item.get("attachmentGuid"),
                "filename": item.get("filename") or item.get("transferName"),
                "mimeType": item.get("mimeType") or item.get("mime_type"),
                "path": item.get("path") or item.get("filename"),
                "transferName": item.get("transferName") or item.get("filename"),
                "totalBytes": item.get("totalBytes") or item.get("total_bytes"),
            }
        )
    return attachments


def _normalized_message_data(message: dict[str, Any]) -> dict[str, Any]:
    chat = message_chat(message)
    timestamp = _coerce_message_timestamp(message.get("dateCreated") or message.get("date"))
    guid = message.get("guid")
    if not isinstance(guid, str) or not guid or timestamp is None:
        raise ValueError("Recovery message requires a GUID and timestamp")

    handle = handle_value(message.get("handle"))
    participants = participant_values(message)
    if handle and handle not in participants:
        participants.append(handle)
        participants.sort()

    chat_guid = message.get("chatGuid") or chat.get("guid")
    chat_display_name = (
        message.get("chatDisplayName")
        or chat.get("displayName")
        or chat.get("name")
        or handle
        or chat_guid
    )

    return {
        "guid": str(guid),
        "dateCreated": int(_parse_iso_timestamp(timestamp).timestamp() * 1000),
        "text": str(message.get("text") or ""),
        "isFromMe": bool(message.get("isFromMe")),
        "chatGuid": str(chat_guid or "unknown-chat"),
        "chatDisplayName": str(chat_display_name or "unknown-chat"),
        "displayName": str(chat_display_name or "unknown-chat"),
        "handle": handle,
        "participants": participants,
        "attachments": _extract_attachments(message),
    }


def _query_url(server_url: str, password: str) -> str:
    base = server_url.rstrip("/")
    return f"{base}/api/v1/message/query?{urlencode({'guid': password})}"


def _query_body(
    *,
    after: datetime,
    before: datetime,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "with": ["chat", "chat.participants", "attachment", "handle"],
        "after": int(after.timestamp() * 1000),
        "before": int(before.timestamp() * 1000),
        "sort": "ASC",
    }


def _message_rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("Recovery response requires an object")
    data = payload.get("data")
    if isinstance(data, dict):
        if any(key in data for key in ("guid", "dateCreated", "date")):
            data = [data]
        else:
            data = next(
                (
                    data[key]
                    for key in ("data", "items", "messages", "results", "rows")
                    if key in data
                ),
                None,
            )
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("Recovery response requires a message list")
    return data


def _existing_guids(paths: RecallPaths, account: str) -> set[str]:
    values: set[str] = set()
    # Receipt day can differ from message day. Retained bundles themselves are not scanned.
    for path in sorted((paths.raw / "bluebubbles").glob("????-??-??/events.jsonl")):
        for row in read_jsonl(path):
            if row.get("account") != account or row.get("event_type") not in {
                "new-message",
                "historical-message",
                "recovered-message",
            }:
                continue
            payload = row.get("payload") or {}
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(data, dict) and data.get("guid"):
                values.add(str(data["guid"]))
        # A prior failed fsync may have left a complete but uncommitted raw record.
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return values


def recover_bluebubbles_messages(
    paths: RecallPaths,
    *,
    account: str,
    server_url: str,
    password: str,
    since: str | None = None,
    until: str | None = None,
    recover_hours: int = DEFAULT_RECOVERY_HOURS,
    page_size: int = DEFAULT_RECOVERY_PAGE_SIZE,
    overlap_seconds: int = DEFAULT_RECOVERY_OVERLAP_SECONDS,
    client: BlueBubblesRecoveryHttpClient | None = None,
) -> BlueBubblesRecoveryResult:
    with bluebubbles_writer(paths):
        return _recover_bluebubbles_messages(
            paths,
            account=account,
            server_url=server_url,
            password=password,
            since=since,
            until=until,
            recover_hours=recover_hours,
            page_size=page_size,
            overlap_seconds=overlap_seconds,
            client=client,
        )


def _recover_bluebubbles_messages(
    paths: RecallPaths,
    *,
    account: str,
    server_url: str,
    password: str,
    since: str | None = None,
    until: str | None = None,
    recover_hours: int = DEFAULT_RECOVERY_HOURS,
    page_size: int = DEFAULT_RECOVERY_PAGE_SIZE,
    overlap_seconds: int = DEFAULT_RECOVERY_OVERLAP_SECONDS,
    client: BlueBubblesRecoveryHttpClient | None = None,
) -> BlueBubblesRecoveryResult:
    paths.ensure_directories()
    cursor = get_connector_cursor(
        paths,
        source="bluebubbles",
        account=account,
        cursor_key=BLUEBUBBLES_CURSOR_KEY,
    )
    cursor_before = cursor.cursor_value if cursor is not None else None
    window_end = _parse_iso_timestamp(until) if until else datetime.now(timezone.utc)
    if since is not None:
        window_start = _parse_iso_timestamp(since)
    else:
        floor = window_end - timedelta(hours=recover_hours)
        if cursor_before is None:
            window_start = floor
        else:
            cursor_start = _parse_iso_timestamp(cursor_before) - timedelta(seconds=overlap_seconds)
            window_start = max(cursor_start, floor)

    url = _query_url(server_url, password)
    httpx = importlib.import_module("httpx")
    http_client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    owns_client = client is None
    recovered_messages = 0
    skipped_existing = 0
    dates_written: list[str] = []
    last_seen_timestamp = cursor_before
    offset = 0
    pages_fetched = 0

    try:
        known_guids = _existing_guids(paths, account)
        while True:
            response = http_client.post(
                url,
                json=_query_body(
                    after=window_start,
                    before=window_end,
                    offset=offset,
                    limit=page_size,
                ),
            )
            response.raise_for_status()
            rows = _message_rows_from_payload(response.json())
            pages_fetched += 1
            if not rows:
                break

            # Validate the entire page before any capture or coverage claim.
            messages = [_normalized_message_data(row) for row in rows]
            for data in messages:
                timestamp = _coerce_message_timestamp(data["dateCreated"])
                if timestamp is None:
                    raise ValueError("Recovery message timestamp missing")
                if last_seen_timestamp is None or timestamp > last_seen_timestamp:
                    last_seen_timestamp = timestamp
                date = local_date_for_timestamp(timestamp)
                message_guid = str(data["guid"])
                if message_guid in known_guids:
                    skipped_existing += 1
                    continue

                envelope = {
                    "received_at": timestamp,
                    "source": "bluebubbles",
                    "account": account,
                    "capture_mode": "recovery",
                    "event_type": "historical-message",
                    "payload": {
                        "type": "historical-message",
                        "data": data,
                    },
                }
                _append_bluebubbles_envelope(
                    paths, date=date, envelope=envelope, update_cursor=False
                )
                known_guids.add(message_guid)
                recovered_messages += 1
                if date not in dates_written:
                    dates_written.append(date)

            if len(rows) < page_size:
                break
            offset += len(rows)
    except Exception as exc:
        raise RuntimeError(safe_error(exc)) from None
    finally:
        if owns_client:
            http_client.close()

    cursor_after = None
    if last_seen_timestamp is not None:
        stored_cursor = advance_bluebubbles_cursor(
            paths, account=account, timestamp=last_seen_timestamp
        )
        cursor_after = (
            stored_cursor.cursor_value if stored_cursor is not None else last_seen_timestamp
        )

    return BlueBubblesRecoveryResult(
        account=account,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        window_start=_iso_timestamp(window_start),
        window_end=_iso_timestamp(window_end),
        recovered_messages=recovered_messages,
        skipped_existing=skipped_existing,
        dates_written=dates_written,
        pages_fetched=pages_fetched,
    )
