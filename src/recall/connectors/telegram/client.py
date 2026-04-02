from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_type: str
    payload: dict[str, Any]
    update_id: int | None = None
    received_at: str | None = None


class TelegramCaptureClient(Protocol):
    def get_updates(
        self,
        *,
        after_update_id: int | None = None,
        limit: int | None = None,
    ) -> list[TelegramUpdate]: ...


def _record_to_update(record: dict[str, Any]) -> TelegramUpdate:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Telegram update record payload must be an object")

    update_type = record.get("update_type")
    if not isinstance(update_type, str) or not update_type:
        raise ValueError("Telegram update record update_type must be a non-empty string")

    update_id = record.get("update_id")
    if update_id is not None:
        update_id = int(update_id)

    received_at = record.get("received_at")
    if received_at is not None:
        received_at = str(received_at)

    return TelegramUpdate(
        update_type=update_type,
        payload=payload,
        update_id=update_id,
        received_at=received_at,
    )


def load_updates_file(path: Path) -> list[TelegramUpdate]:
    updates: list[TelegramUpdate] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Telegram update row in {path} must be a JSON object")
            updates.append(_record_to_update(record))
    return updates


@dataclass(slots=True)
class FileTelegramClient:
    updates: list[TelegramUpdate]

    @classmethod
    def from_path(cls, path: Path) -> "FileTelegramClient":
        return cls(updates=load_updates_file(path))

    def get_updates(
        self,
        *,
        after_update_id: int | None = None,
        limit: int | None = None,
    ) -> list[TelegramUpdate]:
        rows: Iterable[TelegramUpdate] = self.updates
        if after_update_id is not None:
            rows = [row for row in rows if row.update_id is None or row.update_id > after_update_id]
        if limit is not None:
            rows = list(rows)[:limit]
        return list(rows)
