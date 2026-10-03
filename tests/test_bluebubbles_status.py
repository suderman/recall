from __future__ import annotations

import json
import sqlite3

from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths


def status(root, *args):
    return CliRunner().invoke(app, ["state", "show", "bluebubbles", "--root", str(root), *args])


def test_empty_status_does_not_create_a_store(tmp_path):
    root = tmp_path / "absent"
    result = status(root, "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["receipts"] == report["unique_message_guids"] == 0
    assert report["cursors"] == report["errors"] == []
    assert report["last_received_at"] is None
    assert not root.exists()
    assert "cursor_state=empty" in status(root).output
    assert not root.exists()


def test_status_reads_legacy_schema_without_migration(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    paths.state.mkdir(parents=True)
    with sqlite3.connect(paths.database) as db:
        db.executescript(
            "CREATE TABLE resolutions (resolution_id TEXT);"
            "CREATE TABLE connector_cursors (source TEXT, account TEXT, cursor_key TEXT,"
            " cursor_value TEXT, updated_at TEXT);"
            "INSERT INTO connector_cursors VALUES ('bluebubbles','personal',"
            " 'last_message_timestamp','2026-10-03T12:00:00Z','2026-10-03T12:00:00Z');"
        )
    before = paths.database.read_bytes()
    result = status(tmp_path, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["cursors"][0]["cursor_key"] == "last_message_timestamp"
    assert paths.database.read_bytes() == before
    with sqlite3.connect(paths.database) as db:
        assert len(db.execute("PRAGMA table_info(resolutions)").fetchall()) == 1


def test_receipts_are_account_scoped_and_not_complete_coverage(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    row = {
        "source": "bluebubbles",
        "account": "personal",
        "capture_mode": "webhook",
        "received_at": "2026-10-03T12:00:00Z",
        "event_type": "new-message",
        "payload": {"type": "new-message", "data": {"guid": "one", "text": "FAKE_SECRET"}},
    }
    raw = paths.raw_capture_dir("bluebubbles", "2026-10-03") / "events.jsonl"
    write_jsonl(
        raw,
        [
            row,
            {**row, "received_at": "2026-10-03T12:01:00Z"},
            {**row, "account": "other"},
            {**row, "event_type": "typing-indicator", "payload": {"type": "typing-indicator"}},
        ],
    )
    before = raw.read_bytes()
    result = status(tmp_path, "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["receipts"] == 3 and report["unique_message_guids"] == 1
    assert report["message_receipts"] == 2
    assert report["last_received_at"] == "2026-10-03T12:01:00Z"
    assert report["raw_dates"] == ["2026-10-03"]
    assert report["capture_modes"] == {"webhook": 3}
    assert report["last_webhook_received_at"] == "2026-10-03T12:01:00Z"
    assert "not proof" in report["coverage_note"]
    assert "FAKE_SECRET" not in result.output
    assert raw.read_bytes() == before
    assert json.loads(status(tmp_path, "--account", "other", "--json").output)["receipts"] == 1


def test_partial_log_and_unreadable_database_are_reported_without_repair(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    paths.state.mkdir(parents=True)
    paths.database.write_bytes(b"invalid database FAKE_SECRET")
    raw = paths.raw_capture_dir("bluebubbles", "2026-10-03") / "events.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"FAKE_SECRET":')
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = status(tmp_path, "--json")
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert {e["kind"] for e in report["errors"]} == {
        "cursor_state_unreadable",
        "raw_record_invalid",
    }
    assert "FAKE_SECRET" not in result.output
    assert {p: p.read_bytes() for p in before} == before


def test_import_and_unknown_modes_do_not_claim_recent_webhook_delivery(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    base = {
        "source": "bluebubbles",
        "account": "personal",
        "event_type": "historical-message",
        "payload": {"data": {"guid": "fixture"}},
    }
    raw = paths.raw_capture_dir("bluebubbles", "2026-10-03") / "events.jsonl"
    write_jsonl(
        raw,
        [
            {**base, "capture_mode": "import", "received_at": "2026-10-03T12:00:00Z"},
            {**base, "capture_mode": {"FAKE_SECRET": True}, "received_at": "2026-10-03T13:00:00Z"},
            {**base, "capture_mode": "webhook", "received_at": "2026-10-01T12:00:00Z"},
        ],
    )
    result = status(tmp_path, "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["capture_modes"] == {"import": 1, "other": 1, "webhook": 1}
    assert report["last_received_at"] == "2026-10-03T13:00:00Z"
    assert report["last_webhook_received_at"] == "2026-10-01T12:00:00Z"
    assert "FAKE_SECRET" not in result.output


def test_bad_receipt_shape_is_not_counted_as_valid_evidence(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    raw = paths.raw_capture_dir("bluebubbles", "2026-10-03") / "events.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "[]\n"
        + json.dumps({"source": "bluebubbles", "account": "personal", "received_at": "bad"})
        + "\n"
    )
    result = status(tmp_path, "--json")
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["receipts"] == 0 and len(report["errors"]) == 2
