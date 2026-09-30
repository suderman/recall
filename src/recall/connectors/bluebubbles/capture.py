from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.state import ConnectorCursor, get_connector_cursor, set_connector_cursor

BLUEBUBBLES_CURSOR_KEY = "last_message_timestamp"


@dataclass(frozen=True, slots=True)
class BlueBubblesCaptureResult:
    date: str
    raw_dir: Path
    events_path: Path
    cursor: ConnectorCursor | None = None


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_today() -> str:
    return datetime.now().astimezone().date().isoformat()


def local_date_for_timestamp(timestamp: str) -> str:
    normalized = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone().date().isoformat()


def _normalize_cursor_timestamp(timestamp: str) -> str:
    normalized = timestamp.replace("Z", "+00:00")
    return (
        datetime.fromisoformat(normalized)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _millis_to_iso(value: int | float | str | None) -> str | None:
    if value is None:
        return None

    numeric = float(value)
    if numeric > 1_000_000_000_000_000:
        numeric /= 1_000_000_000
    elif numeric > 10_000_000_000:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _message_timestamp_from_envelope(envelope: dict[str, Any]) -> str | None:
    event_type = str(envelope.get("event_type") or "")
    capture_mode = str(envelope.get("capture_mode") or "")
    if capture_mode not in {"webhook", "recovery"}:
        return None
    if event_type not in {"new-message", "historical-message", "recovered-message"}:
        return None

    payload = envelope.get("payload") or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    timestamp = _millis_to_iso(data.get("dateCreated") or data.get("date"))
    if timestamp is not None:
        return timestamp
    received_at = envelope.get("received_at")
    if isinstance(received_at, str) and received_at:
        return _normalize_cursor_timestamp(received_at)
    return None


def advance_bluebubbles_cursor(
    paths: RecallPaths,
    *,
    account: str,
    timestamp: str | None,
) -> ConnectorCursor | None:
    if not timestamp:
        return None

    normalized_timestamp = _normalize_cursor_timestamp(timestamp)
    current = get_connector_cursor(
        paths,
        source="bluebubbles",
        account=account,
        cursor_key=BLUEBUBBLES_CURSOR_KEY,
    )
    if (
        current is not None
        and _normalize_cursor_timestamp(current.cursor_value) >= normalized_timestamp
    ):
        return current

    return set_connector_cursor(
        paths,
        source="bluebubbles",
        account=account,
        cursor_key=BLUEBUBBLES_CURSOR_KEY,
        cursor_value=normalized_timestamp,
        updated_at=normalized_timestamp,
    )


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path]:
    raw_dir = paths.raw_capture_dir("bluebubbles", date)
    return raw_dir, raw_dir / "events.jsonl"


def append_bluebubbles_envelope(
    paths: RecallPaths,
    *,
    date: str,
    envelope: dict[str, Any],
) -> BlueBubblesCaptureResult:
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    write_jsonl(events_path, [envelope], append=True)
    cursor = advance_bluebubbles_cursor(
        paths,
        account=str(envelope.get("account") or "personal"),
        timestamp=_message_timestamp_from_envelope(envelope),
    )
    return BlueBubblesCaptureResult(
        date=date, raw_dir=raw_dir, events_path=events_path, cursor=cursor
    )


def append_bluebubbles_event(
    paths: RecallPaths,
    *,
    account: str,
    payload: dict[str, Any],
    received_at: str | None = None,
) -> BlueBubblesCaptureResult:
    paths.ensure_directories()
    timestamp = received_at or current_timestamp()
    date = local_date_for_timestamp(timestamp)
    raw_dir, events_path = raw_capture_paths(paths, date)
    envelope = {
        "received_at": timestamp,
        "source": "bluebubbles",
        "account": account,
        "capture_mode": "webhook",
        "event_type": payload.get("type", "unknown"),
        "payload": payload,
    }
    return append_bluebubbles_envelope(paths, date=date, envelope=envelope)
