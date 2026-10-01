from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_tdlib_updates import message
from test_telegram import FakeTdlibTransport, _tdlib_settings

from recall.connectors.telegram.capture import capture_telegram_updates
from recall.connectors.telegram.tdlib import TdlibTelegramClient
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor


def client(tmp_path, responses=()):
    transport = FakeTdlibTransport(
        list(responses),
        request_responses={
            ("getChat", 1001): {"@type": "chat", "id": 1001, "title": "Ariel"},
            ("getUser", 42): {"@type": "user", "id": 42, "first_name": "Ariel"},
        },
    )
    result = TdlibTelegramClient(
        transport=transport, settings=_tdlib_settings(tmp_path), receive_timeout_seconds=0.01
    )
    result._ready = True
    return result


def raw_rows(paths):
    return [row for file in paths.raw.rglob("updates.jsonl") for row in read_jsonl(file)]


def test_restart_preserves_emitted_and_interleaved_receipts(tmp_path):
    original = client(tmp_path, [message(1), message(2)])
    first = original.get_updates(after_update_id=631, limit=1)[0]
    original.close()
    resumed = client(tmp_path)
    try:
        updates = resumed.get_updates(after_update_id=631)
        assert len(updates) == 2
        assert updates[0] == first
        assert [row.update_id for row in updates] == [632, 633]
        assert [row.payload["message"]["id"] for row in updates] == [1, 2]
        assert all(row.receipt_id is not None for row in updates)
    finally:
        resumed.close()


@pytest.mark.parametrize("failure", ["raw", "fsync", "cursor", "ack"])
def test_capture_failures_replay_once_with_same_payload(tmp_path, monkeypatch, failure):
    import recall.connectors.telegram.capture as capture

    paths = RecallPaths.from_root(tmp_path / "archive")
    original = client(tmp_path, [message(1), message(2)])
    if failure == "raw":
        target, name = capture, "append_telegram_envelope"
    elif failure == "fsync":
        target, name = capture.os, "fsync"
    elif failure == "cursor":
        target, name = capture, "set_connector_cursor"
    else:
        target, name = original, "acknowledge_update"
    with monkeypatch.context() as patch:
        patch.setattr(target, name, lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            capture_telegram_updates(
                paths, client=original, account="personal", after_update_id=631
            )
    frozen = raw_rows(paths)
    original.close()
    resumed = client(tmp_path)
    try:
        result = capture_telegram_updates(
            paths, client=resumed, account="personal", after_update_id=631
        )
        rows = raw_rows(paths)
        assert len(rows) == 2 and len({r["update_id"] for r in rows}) == 2
        assert rows[: len(frozen)] == frozen
        assert result.last_update_id == 633
    finally:
        resumed.close()
    empty = client(tmp_path)
    try:
        assert empty.get_updates(after_update_id=633) == []
    finally:
        empty.close()
    cursor = get_connector_cursor(
        paths, source="telegram", account="personal", cursor_key="last_update_id"
    )
    assert cursor is not None and cursor.cursor_value == "633"


def test_unacknowledged_batch_retries_in_same_client(tmp_path):
    original = client(tmp_path, [message(1), message(2)])
    try:
        first = original.get_updates(after_update_id=631, limit=1)
        assert original.get_updates(after_update_id=631, limit=1) == first
        assert first[0].receipt_id is not None
        original.acknowledge_update(first[0].receipt_id)
        assert original.get_updates(after_update_id=632)[0].update_id == 633
    finally:
        original.close()


@pytest.mark.parametrize("stage", ["received", "enriched", "raw", "cursor"])
def test_hard_process_exit_retains_receipts(tmp_path, stage):
    code = """
import os, sys
from pathlib import Path
from test_tdlib_pending import client
from recall.connectors.telegram.capture import capture_telegram_updates
from recall.storage.paths import RecallPaths
import recall.connectors.telegram.capture as capture
root=Path(sys.argv[1]); stage=sys.argv[2]
from test_tdlib_updates import message
session=client(root, [message(1), message(2)])
if stage == "received":
    session._pending.seed_id(10)
    session._receive(0)
elif stage == "enriched":
    session.get_updates(after_update_id=10, limit=1)
else:
    if stage == "raw":
        capture.set_connector_cursor=lambda *a, **k: os._exit(42)
    else:
        session.acknowledge_update=lambda *a, **k: os._exit(42)
    capture_telegram_updates(RecallPaths.from_root(root/"archive"), client=session,
                             account="personal", after_update_id=10)
os._exit(42)
"""
    project = Path(__file__).parents[1]
    environment = {**os.environ, "PYTHONPATH": f"{project / 'tests'}:{project / 'src'}"}
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), stage],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 42, process.stderr
    paths = RecallPaths.from_root(tmp_path / "archive")
    frozen = raw_rows(paths)
    resumed = client(tmp_path)
    try:
        result = capture_telegram_updates(
            paths, client=resumed, account="personal", after_update_id=10
        )
        expected = 1 if stage == "received" else 2
        assert len(raw_rows(paths)) == expected
        assert raw_rows(paths)[: len(frozen)] == frozen
        assert result.last_update_id == 10 + expected
    finally:
        resumed.close()


def test_queue_scope_permissions_symlinks_and_version(tmp_path):
    session = client(tmp_path)
    session.close()
    path = tmp_path / "tdlib/pending.sqlite3"
    assert path.stat().st_mode & 0o777 == 0o600
    settings = replace(_tdlib_settings(tmp_path), account="different")
    with pytest.raises(ValueError, match="another account"):
        TdlibTelegramClient(transport=FakeTdlibTransport([]), settings=settings)
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="version"):
        client(tmp_path)
    path.unlink()
    external = tmp_path / "external"
    external.write_text("preserve")
    path.symlink_to(external)
    with pytest.raises(ValueError, match="symlinks"):
        client(tmp_path)
    assert external.read_text() == "preserve"


def test_capture_acknowledges_only_saved_part_of_batch(tmp_path, monkeypatch):
    import recall.connectors.telegram.capture as capture

    paths = RecallPaths.from_root(tmp_path / "archive")
    original = client(tmp_path, [message(1), message(2)])
    append = capture.append_telegram_envelope

    def fail_second(*args, **kwargs):
        if kwargs["envelope"]["update_id"] == 12:
            raise OSError("second raw write")
        return append(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(capture, "append_telegram_envelope", fail_second)
        with pytest.raises(OSError, match="second raw write"):
            capture_telegram_updates(paths, client=original, account="personal", after_update_id=10)
    original.close()
    resumed = client(tmp_path)
    try:
        updates = resumed.get_updates(after_update_id=11)
        assert len(updates) == 1 and updates[0].update_id == 12
    finally:
        resumed.close()


def test_one_writer_and_queue_corruption_fail_closed(tmp_path):
    original = client(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            client(tmp_path)
    finally:
        original.close()
    path = tmp_path / "tdlib/pending.sqlite3"
    path.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        client(tmp_path)
    assert path.read_bytes() == b"not sqlite"


def test_malformed_raw_append_retains_pending_receipt(tmp_path):
    from recall.connectors.telegram.capture import local_date_for_timestamp

    paths = RecallPaths.from_root(tmp_path / "archive")
    original = client(tmp_path, [message(1)])
    update = original.get_updates(after_update_id=4, limit=1)[0]
    assert update.received_at is not None
    directory = paths.raw_capture_dir("telegram", local_date_for_timestamp(update.received_at))
    directory.mkdir(parents=True)
    file = directory / "updates.jsonl"
    file.write_text('{"interrupted":')
    original.close()
    resumed = client(tmp_path)
    try:
        with pytest.raises(ValueError, match="Invalid JSONL"):
            capture_telegram_updates(paths, client=resumed, account="personal", after_update_id=4)
    finally:
        resumed.close()
    assert file.read_text() == '{"interrupted":'
    again = client(tmp_path)
    try:
        assert again.get_updates(after_update_id=4)[0] == update
    finally:
        again.close()


def test_conflicting_raw_record_is_not_acknowledged(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "archive")
    original = client(tmp_path, [message(1)])
    update = original.get_updates(after_update_id=4, limit=1)[0]
    from recall.connectors.telegram.capture import append_telegram_update

    append_telegram_update(
        paths,
        account="personal",
        payload={"wrong": True},
        update_type=update.update_type,
        update_id=5,
        received_at=update.received_at,
    )
    original.close()
    resumed = client(tmp_path)
    try:
        with pytest.raises(ValueError, match="Conflicting"):
            capture_telegram_updates(paths, client=resumed, account="personal", after_update_id=4)
    finally:
        resumed.close()
    assert raw_rows(paths)[0]["payload"] == {"wrong": True}
    again = client(tmp_path)
    try:
        assert again.get_updates(after_update_id=4)[0] == update
    finally:
        again.close()
