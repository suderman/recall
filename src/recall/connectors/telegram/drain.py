"""Drain retained receipts using only saved Telegram observations."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from recall.connectors.telegram.client import TelegramUpdate
from recall.connectors.telegram.pending import PendingUpdates
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths


class PendingTelegramClient:
    def __init__(self, paths: RecallPaths, *, account: str, directory: Path) -> None:
        if not directory.resolve().is_relative_to(paths.root):
            raise ValueError("Offline queue must be inside the selected root")
        if not (directory / "pending.sqlite3").is_file():
            raise FileNotFoundError("No retained Telegram queue to drain")
        self._pending = PendingUpdates(directory, account=account)
        self._chats: dict[int, dict[str, Any]] = {}
        self._users: dict[int, dict[str, Any]] = {}
        try:
            for path in sorted((paths.raw / "telegram").glob("*/updates.jsonl")):
                for row in read_jsonl(path):
                    if row.get("account") == account:
                        self._observe(row["payload"], row["received_at"])
            for row in self._pending.database.execute(
                "SELECT payload, received_at FROM receipts ORDER BY receipt_id"
            ):
                self._observe(json.loads(row["payload"]), row["received_at"])
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._pending.close()

    @property
    def remaining(self) -> int:
        return self._pending.database.execute("SELECT count(*) FROM receipts").fetchone()[0]

    def _observe(self, payload: dict[str, Any], received_at: str) -> None:
        context = payload.get("saved_context")
        if isinstance(context, dict) and context.get("method") == "saved-telegram-observations-v1":
            for observation in context["observations"]:
                self._observe(observation["payload"], observation["received_at"])
            return
        for kind, saved in [("chat", self._chats), ("user", self._users)]:
            objects = []
            if isinstance(payload.get(kind), dict):
                objects.append(payload[kind])
            if kind == "user":
                objects.extend(payload.get("users") or [])
            for value in objects:
                if not isinstance(value, dict) or value.get("id") is None:
                    continue
                key = int(value["id"])
                previous = saved.get(key)
                if previous is None or received_at > previous["received_at"]:
                    saved[key] = {"received_at": received_at, "payload": {kind: value}}

    def _enrich(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message")
        if not isinstance(message, dict):
            return payload
        enriched = dict(payload)
        observations = []
        chat = payload.get("chat")
        if not isinstance(chat, dict):
            chat_id = message.get("chat_id")
            observation = self._chats.get(int(chat_id)) if chat_id is not None else None
            if observation is not None:
                chat = observation["payload"]["chat"]
                enriched["chat"] = chat
                observations.append(observation)
        user_ids = set()
        if isinstance(chat, dict):
            chat_type = chat.get("type") or {}
            if chat_type.get("@type") in {"chatTypePrivate", "chatTypeSecret"}:
                if chat_type.get("user_id") is not None:
                    user_ids.add(int(chat_type["user_id"]))
        sender = message.get("sender_id") or {}
        if sender.get("@type") == "messageSenderUser" and sender.get("user_id") is not None:
            user_ids.add(int(sender["user_id"]))
        users = list(payload.get("users") or [])
        known = {int(user["id"]) for user in users}
        for user_id in sorted(user_ids - known):
            observation = self._users.get(user_id)
            if observation is not None:
                users.append(observation["payload"]["user"])
                observations.append(observation)
        if users:
            enriched["users"] = users
        if observations:
            # Embed the saved objects and observation times so later acknowledgements
            # cannot erase the evidence for these labels. They are not event-time names.
            enriched["saved_context"] = {
                "method": "saved-telegram-observations-v1",
                "observations": observations,
            }
        return enriched

    def get_updates(
        self, *, after_update_id: int | None = None, limit: int | None = None
    ) -> list[TelegramUpdate]:
        if limit is None or limit < 1:
            raise ValueError("Offline drain requires a positive update limit")
        self._pending.seed_id(after_update_id)
        updates = []
        for receipt_id in self._pending.receipt_ids()[:limit]:
            update, frozen = self._pending.prepare(receipt_id)
            if not frozen:
                payload = self._enrich(update.payload)
                self._pending.complete(receipt_id, payload)
                update = replace(update, payload=payload)
            updates.append(update)
        return updates

    def acknowledge_update(self, receipt_id: int) -> None:
        self._pending.acknowledge(receipt_id)
