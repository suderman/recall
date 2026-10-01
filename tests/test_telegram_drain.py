from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.connectors.telegram.capture import capture_telegram_updates
from recall.connectors.telegram.drain import PendingTelegramClient
from recall.connectors.telegram.pending import PendingUpdates
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

STAMP = "2026-10-01T20:00:00Z"


def seed(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    paths.ensure_directories()
    directory = paths.state / "telegram/tdlib/personal"
    queue = PendingUpdates(directory, account="personal")
    queue.seed_id(10)
    message = {
        "@type": "updateNewMessage",
        "message": {
            "id": 1,
            "chat_id": 100,
            "date": 1775155200,
            "sender_id": {"@type": "messageSenderUser", "user_id": 42},
            "content": {"@type": "messageText", "text": {"text": "Saved text"}},
        },
    }
    queue.append(message, STAMP)
    queue.append(
        {
            "@type": "updateNewChat",
            "chat": {
                "@type": "chat",
                "id": 100,
                "title": "Group",
                "type": {"@type": "chatTypeSupergroup"},
            },
        },
        STAMP,
    )
    queue.append(
        {"@type": "updateUser", "user": {"@type": "user", "id": 42, "first_name": "Ariel"}}, STAMP
    )
    queue.close()
    return paths, directory, message


def test_offline_saved_labels_bounds_and_restart(tmp_path):
    paths, directory, message = seed(tmp_path)
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    update = client.get_updates(after_update_id=10, limit=1)[0]
    assert update.payload["message"] == message["message"]
    assert update.payload["chat"]["title"] == "Group"
    assert update.payload["users"][0]["first_name"] == "Ariel"
    assert update.payload["saved_context"]["observations"][0]["received_at"] == STAMP
    assert client.remaining == 3
    client.close()
    resumed = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        assert asdict(resumed.get_updates(after_update_id=11, limit=1)[0]) == asdict(update)
        first = capture_telegram_updates(paths, client=resumed, account="personal", limit=1)
        assert first.last_update_id == 11 and resumed.remaining == 2
        second = capture_telegram_updates(paths, client=resumed, account="personal", limit=10)
        assert second.last_update_id == 13 and resumed.remaining == 0
        assert resumed.get_updates(limit=1) == []
    finally:
        resumed.close()
    rows = read_jsonl(next(paths.raw.rglob("updates.jsonl")))
    assert len(rows) == 3 and rows[0]["payload"] == update.payload


@pytest.mark.parametrize("failure", ["cursor", "ack"])
def test_failure_keeps_frozen_receipt_and_no_duplicate(tmp_path, monkeypatch, failure):
    import recall.connectors.telegram.capture as capture

    paths, directory, _ = seed(tmp_path)
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    with monkeypatch.context() as patch:
        target, name = (
            (capture, "set_connector_cursor")
            if failure == "cursor"
            else (client, "acknowledge_update")
        )
        patch.setattr(target, name, lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            capture_telegram_updates(paths, client=client, account="personal", limit=1)
    raw = next(paths.raw.rglob("updates.jsonl"))
    before = raw.read_bytes()
    client.close()
    resumed = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        capture_telegram_updates(paths, client=resumed, account="personal", limit=1)
        assert resumed.remaining == 2 and raw.read_bytes() == before
    finally:
        resumed.close()


def test_frozen_payload_never_reenriched_and_invalid_limits(tmp_path):
    paths, directory, message = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    receipt = queue.receipt_ids()[0]
    queue.prepare(receipt)
    queue.complete(receipt, message)
    queue.close()
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        assert client.get_updates(limit=1)[0].payload == message
        for limit in [None, 0, -1]:
            with pytest.raises(ValueError, match="positive"):
                client.get_updates(limit=limit)
    finally:
        client.close()


def test_saved_context_is_account_scoped_and_channel_not_human(tmp_path):
    paths, directory, message = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    message["message"]["sender_id"] = {"@type": "messageSenderChat", "chat_id": 100}
    queue.database.execute("DELETE FROM receipts")
    queue.database.commit()
    queue.append(message, STAMP)
    queue.close()
    write_jsonl(
        paths.raw_capture_dir("telegram", "2026-10-01") / "updates.jsonl",
        [
            {
                "account": "other",
                "received_at": STAMP,
                "payload": {"@type": "updateUser", "user": {"id": 42, "first_name": "Wrong"}},
            }
        ],
    )
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        payload = client.get_updates(limit=1)[0].payload
        assert "users" not in payload and "chat" not in payload and "saved_context" not in payload
    finally:
        client.close()


def test_missing_queue_busy_lock_and_external_path(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    directory = paths.state / "telegram/tdlib/personal"
    with pytest.raises(FileNotFoundError):
        PendingTelegramClient(paths, account="personal", directory=directory)
    assert not directory.exists()
    with pytest.raises(ValueError, match="inside"):
        PendingTelegramClient(paths, account="personal", directory=tmp_path.parent / "outside")
    paths, directory, _ = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    try:
        with pytest.raises(BlockingIOError):
            PendingTelegramClient(paths, account="personal", directory=directory)
    finally:
        queue.close()


def test_cli_offline_without_credentials_or_transport(tmp_path, monkeypatch):
    paths, _, _ = seed(tmp_path)
    import recall.connectors.telegram.cli as cli

    monkeypatch.setattr(cli, "build_tdlib_auth_settings", lambda *a, **k: pytest.fail("auth"))
    monkeypatch.setattr(cli, "TdlibJsonTransport", lambda *a, **k: pytest.fail("transport"))
    for key in ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE_NUMBER"]:
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(
        app, ["capture", "telegram", "drain", "--root", str(paths.root), "--max-updates", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "captured_updates=1" in result.output and "pending_remaining=2" in result.output
    for args in [
        [],
        ["--root", str(paths.root)],
        ["--root", str(paths.root), "--max-updates", "0"],
    ]:
        assert CliRunner().invoke(app, ["capture", "telegram", "drain", *args]).exit_code != 0


def test_raw_context_survives_context_acknowledgement_and_observation_time(tmp_path):
    paths, directory, message = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    queue.database.execute("DELETE FROM receipts WHERE receipt_id>1")
    queue.database.commit()
    queue.close()
    user = {"@type": "user", "id": 42, "first_name": "New label"}
    write_jsonl(
        paths.raw_capture_dir("telegram", "2026-10-01") / "updates.jsonl",
        [
            {
                "account": "personal",
                "received_at": STAMP,
                "payload": {"@type": "updateUser", "user": user},
            }
        ],
    )
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        update = client.get_updates(limit=1)[0]
        assert update.payload["users"] == [user]
        assert update.payload["saved_context"]["observations"] == [
            {"received_at": STAMP, "payload": {"user": user}}
        ]
        assert update.payload["message"] == message["message"]
    finally:
        client.close()


def test_hard_exit_before_raw_capture_retains_frozen_offline_batch(tmp_path):
    paths, directory, _ = seed(tmp_path)
    project = Path(__file__).parents[1]
    code = """
import os,sys
from pathlib import Path
from recall.connectors.telegram.drain import PendingTelegramClient
from recall.storage.paths import RecallPaths
paths=RecallPaths.from_root(Path(sys.argv[1]))
client=PendingTelegramClient(
    paths, account="personal", directory=paths.state/"telegram/tdlib/personal")
client.get_updates(limit=3)
os._exit(73)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(paths.root)],
        env={**os.environ, "PYTHONPATH": str(project / "src")},
        timeout=10,
    )
    assert result.returncode == 73
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        result = capture_telegram_updates(paths, client=client, account="personal", limit=3)
        assert result.last_update_id == 13 and client.remaining == 0
    finally:
        client.close()


def test_derived_labels_keep_original_observation_time_on_restart(tmp_path):
    paths, directory, _ = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    queue.database.execute("DELETE FROM receipts WHERE receipt_id>1")
    queue.database.commit()
    queue.close()
    user = {"@type": "user", "id": 42, "first_name": "Saved name"}
    original_stamp = "2026-09-30T19:00:00Z"
    write_jsonl(
        paths.raw_capture_dir("telegram", "2026-10-01") / "updates.jsonl",
        [
            {
                "account": "personal",
                "received_at": STAMP,
                "payload": {
                    "@type": "updateNewMessage",
                    "users": [user],
                    "saved_context": {
                        "method": "saved-telegram-observations-v1",
                        "observations": [
                            {"received_at": original_stamp, "payload": {"user": user}}
                        ],
                    },
                },
            }
        ],
    )
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        update = client.get_updates(limit=1)[0]
        assert update.payload["users"] == [user]
        assert update.payload["saved_context"]["observations"][0]["received_at"] == original_stamp
    finally:
        client.close()


def test_malformed_identity_stops_without_acknowledgement(tmp_path):
    paths, directory, message = seed(tmp_path)
    queue = PendingUpdates(directory, account="personal")
    queue.database.execute("DELETE FROM receipts")
    queue.database.commit()
    message["message"]["sender_id"]["user_id"] = "not-a-number"
    queue.append(message, STAMP)
    queue.close()
    client = PendingTelegramClient(paths, account="personal", directory=directory)
    try:
        with pytest.raises(ValueError):
            capture_telegram_updates(paths, client=client, account="personal", limit=1)
        assert client.remaining == 1
        update, frozen = client._pending.prepare(client._pending.receipt_ids()[0])
        assert not frozen and update.payload == message
        assert not list(paths.raw.rglob("updates.jsonl"))
    finally:
        client.close()
