"""Durable TDLib receipts, retained until the archive acknowledges them."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from recall.connectors.telegram.client import TelegramUpdate


class PendingUpdates:
    def __init__(self, directory: Path, *, account: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "pending.sqlite3"
        lock_path = directory / "pending.lock"
        if path.is_symlink() or lock_path.is_symlink():
            raise ValueError("Telegram pending files must not be symlinks")
        self._lock = lock_path.open("a")
        self._lock_path = lock_path
        self._db: sqlite3.Connection | None = None
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_path.chmod(0o600)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            self._db = sqlite3.connect(path)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA synchronous=EXTRA")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported Telegram pending queue version")
            with self._db:
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS state "
                    "(account TEXT PRIMARY KEY, last_id INTEGER NOT NULL)"
                )
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS receipts ("
                    "receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, update_id INTEGER UNIQUE, "
                    "received_at TEXT NOT NULL, payload TEXT NOT NULL, "
                    "enriched INTEGER NOT NULL DEFAULT 0)"
                )
                accounts = [row[0] for row in self._db.execute("SELECT account FROM state")]
                if accounts and accounts != [account]:
                    raise ValueError("Telegram pending queue belongs to another account")
                self._db.execute("INSERT OR IGNORE INTO state VALUES (?, 0)", (account,))
                self._db.execute("PRAGMA user_version=1")
            path.chmod(0o600)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._lock.close()

    @property
    def database(self) -> sqlite3.Connection:
        assert self._db is not None
        return self._db

    def receipt_ids(self) -> list[int]:
        return [
            row[0]
            for row in self.database.execute("SELECT receipt_id FROM receipts ORDER BY receipt_id")
        ]

    def seed_id(self, after_update_id: int | None) -> None:
        if after_update_id is not None:
            with self.database:
                self.database.execute(
                    "UPDATE state SET last_id=max(last_id, ?)", (after_update_id,)
                )

    def append(self, payload: dict[str, Any], received_at: str) -> int:
        content = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        with self.database:
            row = self.database.execute(
                "INSERT INTO receipts (received_at, payload) VALUES (?, ?) RETURNING receipt_id",
                (received_at, content),
            ).fetchone()
        return row[0]

    def prepare(self, receipt_id: int) -> tuple[TelegramUpdate, bool]:
        with self.database:
            row = self.database.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row["update_id"] is None:
                update_id = self.database.execute(
                    "UPDATE state SET last_id=last_id+1 RETURNING last_id"
                ).fetchone()[0]
                self.database.execute(
                    "UPDATE receipts SET update_id=? WHERE receipt_id=?", (update_id, receipt_id)
                )
                row = self.database.execute(
                    "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
                ).fetchone()
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict) or not str(payload.get("@type", "")).startswith("update"):
            raise ValueError("Invalid Telegram pending payload")
        return TelegramUpdate(
            update_type=payload["@type"],
            payload=payload,
            update_id=row["update_id"],
            received_at=row["received_at"],
            receipt_id=receipt_id,
        ), bool(row["enriched"])

    def complete(self, receipt_id: int, payload: dict[str, Any]) -> None:
        with self.database:
            self.database.execute(
                "UPDATE receipts SET payload=?, enriched=1 WHERE receipt_id=?",
                (json.dumps(payload, ensure_ascii=True, sort_keys=True), receipt_id),
            )

    def acknowledge(self, receipt_id: int) -> None:
        with self.database:
            self.database.execute("DELETE FROM receipts WHERE receipt_id=?", (receipt_id,))
