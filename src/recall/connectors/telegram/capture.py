from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.connectors.telegram.client import TelegramCaptureClient
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.state import ConnectorCursor, set_connector_cursor

DATE_FORMAT = "%Y-%m-%d"
TELEGRAM_CURSOR_KEY = "last_update_id"


@dataclass(frozen=True, slots=True)
class TelegramCaptureResult:
    date: str
    raw_dir: Path
    updates_path: Path
    cursor: ConnectorCursor | None = None


@dataclass(frozen=True, slots=True)
class TelegramCaptureBatchResult:
    account: str
    captured_updates: int
    dates_written: list[str]
    last_update_id: int | None
    cursor: ConnectorCursor | None


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_date_for_timestamp(timestamp: str) -> str:
    normalized = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone().date().isoformat()


def parse_date(value: str) -> str:
    datetime.strptime(value, DATE_FORMAT)
    return value


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path]:
    parse_date(date)
    raw_dir = paths.raw_capture_dir("telegram", date)
    return raw_dir, raw_dir / "updates.jsonl"


def load_update_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Telegram payload in {path} must be a JSON object")
    return payload


def append_telegram_envelope(
    paths: RecallPaths,
    *,
    date: str,
    envelope: dict[str, Any],
) -> TelegramCaptureResult:
    paths.ensure_directories()
    raw_dir, updates_path = raw_capture_paths(paths, date)
    write_jsonl(updates_path, [envelope], append=True)
    # Raw evidence must reach disk before the cursor or pending acknowledgement.
    with updates_path.open("rb") as handle:
        os.fsync(handle.fileno())
    return TelegramCaptureResult(date=date, raw_dir=raw_dir, updates_path=updates_path)


def append_telegram_update(
    paths: RecallPaths,
    *,
    account: str,
    payload: dict[str, Any],
    update_type: str,
    update_id: int | None = None,
    received_at: str | None = None,
    capture_mode: str = "manual",
) -> TelegramCaptureResult:
    paths.ensure_directories()
    timestamp = received_at or current_timestamp()
    date = local_date_for_timestamp(timestamp)
    envelope = {
        "received_at": timestamp,
        "source": "telegram",
        "account": account,
        "capture_mode": capture_mode,
        "update_type": update_type,
        "payload": payload,
    }
    if update_id is not None:
        envelope["update_id"] = int(update_id)

    result = append_telegram_envelope(paths, date=date, envelope=envelope)
    if update_id is None:
        return result

    cursor = set_connector_cursor(
        paths,
        source="telegram",
        account=account,
        cursor_key=TELEGRAM_CURSOR_KEY,
        cursor_value=str(update_id),
        updated_at=timestamp,
    )
    return TelegramCaptureResult(
        date=result.date,
        raw_dir=result.raw_dir,
        updates_path=result.updates_path,
        cursor=cursor,
    )


def capture_telegram_updates(
    paths: RecallPaths,
    *,
    client: TelegramCaptureClient,
    account: str,
    after_update_id: int | None = None,
    limit: int | None = None,
    capture_mode: str = "stream",
) -> TelegramCaptureBatchResult:
    paths.ensure_directories()
    updates = client.get_updates(after_update_id=after_update_id, limit=limit)
    dates_written: list[str] = []
    last_update_id: int | None = None
    cursor: ConnectorCursor | None = None
    saved_days: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}

    for update in updates:
        saved = None
        if update.receipt_id is not None:
            assert update.update_id is not None and update.received_at is not None
            day = local_date_for_timestamp(update.received_at)
            if day not in saved_days:
                _, raw_path = raw_capture_paths(paths, day)
                saved_days[day] = (
                    {
                        (row["account"], row["update_id"]): row
                        for row in read_jsonl(raw_path)
                        if "update_id" in row
                    }
                    if raw_path.exists()
                    else {}
                )
            saved = saved_days[day].get((account, update.update_id))
            if saved is not None:
                expected = {
                    "source": "telegram",
                    "account": account,
                    "update_type": update.update_type,
                    "update_id": update.update_id,
                    "received_at": update.received_at,
                    "payload": update.payload,
                }
                if any(saved.get(key) != value for key, value in expected.items()):
                    raise ValueError(f"Conflicting Telegram raw update {update.update_id}")

        if saved is None:
            result = append_telegram_update(
                paths,
                account=account,
                payload=update.payload,
                update_type=update.update_type,
                update_id=update.update_id,
                received_at=update.received_at,
                capture_mode=capture_mode,
            )
        else:
            day = local_date_for_timestamp(saved["received_at"])
            raw_dir, raw_path = raw_capture_paths(paths, day)
            with raw_path.open("rb") as handle:
                os.fsync(handle.fileno())
            result = TelegramCaptureResult(
                date=day,
                raw_dir=raw_dir,
                updates_path=raw_path,
                cursor=set_connector_cursor(
                    paths,
                    source="telegram",
                    account=account,
                    cursor_key=TELEGRAM_CURSOR_KEY,
                    cursor_value=str(update.update_id),
                    updated_at=update.received_at,
                ),
            )
        if update.receipt_id is not None:
            client.acknowledge_update(update.receipt_id)
        if result.date not in dates_written:
            dates_written.append(result.date)
        if update.update_id is not None:
            last_update_id = update.update_id
        if result.cursor is not None:
            cursor = result.cursor

    return TelegramCaptureBatchResult(
        account=account,
        captured_updates=len(updates),
        dates_written=dates_written,
        last_update_id=last_update_id,
        cursor=cursor,
    )
