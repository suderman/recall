from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from recall.connectors.bluebubbles.capture import append_bluebubbles_event
from recall.connectors.bluebubbles.config import BlueBubblesSourceConfig
from recall.connectors.bluebubbles.entities import sync_bluebubbles_entities
from recall.connectors.bluebubbles.exporter import export_bluebubbles_history
from recall.connectors.bluebubbles.importer import import_bluebubbles_export
from recall.connectors.bluebubbles.normalize import normalize_bluebubbles_day
from recall.connectors.bluebubbles.webhook import create_bluebubbles_webhook_app
from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import read_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bluebubbles" / "new_message.json"
EXPORT_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bluebubbles_export"


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def copy_export_fixture(tmp_path: Path) -> Path:
    export_dir = tmp_path / "bluebubbles-export"
    shutil.copytree(EXPORT_FIXTURE_DIR, export_dir)
    return export_dir


def build_messages_db(tmp_path: Path) -> Path:
    database_path = tmp_path / "chat.db"
    connection = sqlite3.connect(database_path)
    try:
        apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        target = datetime(2026, 3, 31, 21, 31, 7, tzinfo=timezone.utc)
        message_date = int((target - apple_epoch).total_seconds() * 1_000_000_000)

        connection.executescript(
            """
            create table handle (rowid integer primary key, id text);
            create table chat (rowid integer primary key, guid text, display_name text);
            create table message (
              rowid integer primary key,
              guid text,
              text text,
              is_from_me integer,
              handle_id integer,
              date integer
            );
            create table chat_message_join (chat_id integer, message_id integer);
            create table chat_handle_join (chat_id integer, handle_id integer);
            create table attachment (
              rowid integer primary key,
              guid text,
              filename text,
              mime_type text,
              transfer_name text,
              total_bytes integer
            );
            create table message_attachment_join (message_id integer, attachment_id integer);
            """
        )
        connection.execute("insert into handle(rowid, id) values (1, '+15551234567')")
        connection.execute("insert into handle(rowid, id) values (2, 'jon@icloud.com')")
        connection.execute(
            (
                "insert into chat(rowid, guid, display_name) values "
                "(1, 'iMessage;+15551234567', 'Ariel')"
            )
        )
        connection.execute("insert into chat_handle_join(chat_id, handle_id) values (1, 1)")
        connection.execute("insert into chat_handle_join(chat_id, handle_id) values (1, 2)")
        connection.execute(
            (
                "insert into message(rowid, guid, text, is_from_me, handle_id, date) "
                "values (?, ?, ?, ?, ?, ?)"
            ),
            (
                1,
                "p:0/8A5C4A20-3EFA-4AFB-9B41-8D0370F6D2D4",
                "Historical photo https://example.com/old-story",
                0,
                1,
                message_date,
            ),
        )
        connection.execute("insert into chat_message_join(chat_id, message_id) values (1, 1)")
        connection.execute(
            (
                "insert into attachment(rowid, guid, filename, mime_type, "
                "transfer_name, total_bytes) "
                "values (?, ?, ?, ?, ?, ?)"
            ),
            (
                1,
                "at_hist_001",
                "/Users/jon/Library/Messages/Attachments/aa/bb/IMG_0999.jpeg",
                "image/jpeg",
                "IMG_0999.jpeg",
                321000,
            ),
        )
        connection.execute(
            "insert into message_attachment_join(message_id, attachment_id) values (1, 1)"
        )
        connection.commit()
    finally:
        connection.close()
    return database_path


def test_bluebubbles_webhook_captures_raw_event(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    config = BlueBubblesSourceConfig(account="personal", webhook_token="secret")
    app = create_bluebubbles_webhook_app(paths, config)
    client = TestClient(app)

    response = client.post("/bluebubbles/webhook?token=secret", json=load_fixture())

    assert response.status_code == 200
    events_path = paths.raw_capture_dir("bluebubbles", response.json()["date"]) / "events.jsonl"
    assert events_path.exists()
    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["source"] == "bluebubbles"
    assert row["account"] == "personal"
    assert row["event_type"] == "new-message"
    assert row["payload"]["data"]["guid"] == load_fixture()["data"]["guid"]


def test_bluebubbles_webhook_rejects_invalid_token(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    config = BlueBubblesSourceConfig(account="personal", webhook_token="secret")
    app = create_bluebubbles_webhook_app(paths, config)
    client = TestClient(app)

    response = client.post("/bluebubbles/webhook?token=wrong", json=load_fixture())

    assert response.status_code == 401


def test_normalize_bluebubbles_day_writes_events_and_artifacts(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    append_bluebubbles_event(
        paths,
        account="personal",
        payload=load_fixture(),
        received_at="2026-03-31T21:31:07Z",
    )

    event_path, artifact_path = normalize_bluebubbles_day(paths, date="2026-03-31")

    event_record = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    artifact_record = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])

    assert event_record["source"] == "bluebubbles"
    assert event_record["conversation_id"] == "iMessage;+15551234567"
    assert event_record["conversation_label"] == "Ariel"
    assert event_record["text"] == "Photo from bub https://example.com/story"
    assert event_record["source_urls"] == ["https://example.com/story"]
    assert len(event_record["artifact_ids"]) == 1
    assert event_record["sender_identity_id"].startswith("ident_bluebubbles_phone_")
    assert len(event_record["participant_identity_ids"]) == 2
    assert event_record["raw_ref"]["path"] == "data/raw/bluebubbles/2026-03-31/events.jsonl"

    assert artifact_record["source"] == "bluebubbles"
    assert artifact_record["source_object_id"] == "at_001"
    assert artifact_record["filename"] == "IMG_1001.jpeg"
    assert artifact_record["mime_type"] == "image/jpeg"
    assert artifact_record["size_bytes"] == 482193
    assert artifact_record["download_status"] == "not_requested"
    assert artifact_record["event_ids"] == [event_record["event_id"]]
    assert artifact_record["remote_locators"] == [
        {
            "kind": "attachment_path",
            "value": "/Users/jon/Library/Messages/Attachments/ab/cd/IMG_1001.jpeg",
        },
        {
            "kind": "transferName",
            "value": "IMG_1001.jpeg",
        },
    ]


def test_sync_bluebubbles_entities_persists_handles_and_aliases(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    append_bluebubbles_event(
        paths,
        account="personal",
        payload=load_fixture(),
        received_at="2026-03-31T21:31:07Z",
    )

    result = sync_bluebubbles_entities(paths, date="2026-03-31")

    assert result.identities_synced == 2
    assert result.aliases_synced == 2

    with sqlite3.connect(paths.database) as connection:
        identities = connection.execute(
            "select source, kind, value, person_id from identities order by kind, value"
        ).fetchall()
        aliases = connection.execute(
            (
                "select identity_id, value, source from identity_aliases "
                "order by identity_id, value, source"
            )
        ).fetchall()

    assert identities == [
        ("bluebubbles", "email", "jon@icloud.com", None),
        ("bluebubbles", "phone", "+15551234567", None),
    ]
    assert len(aliases) == 2
    assert {value for _, value, _ in aliases} == {"Ariel"}
    assert {source for _, _, source in aliases} == {"bluebubbles_chat_display_name"}


def test_sync_bluebubbles_entities_is_idempotent(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    append_bluebubbles_event(
        paths,
        account="personal",
        payload=load_fixture(),
        received_at="2026-03-31T21:31:07Z",
    )

    first = sync_bluebubbles_entities(paths, date="2026-03-31")
    second = sync_bluebubbles_entities(paths, date="2026-03-31")

    assert first.identities_synced == second.identities_synced == 2
    assert first.aliases_synced == second.aliases_synced == 2

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]

    assert identity_count == 2
    assert alias_count == 2


def test_import_bluebubbles_export_preserves_bundle_and_partitions_raw_days(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = copy_export_fixture(tmp_path)

    result = import_bluebubbles_export(paths, export_path=export_dir, account="personal")

    assert result.import_id == "bb_hist_20260331"
    assert result.messages_imported == 1
    assert result.dates_written == ["2026-03-31"]
    assert (result.import_dir / "manifest.json").exists()
    assert (result.import_dir / "messages.jsonl").exists()

    raw_day_file = paths.raw_capture_dir("bluebubbles", "2026-03-31") / "events.jsonl"
    row = json.loads(raw_day_file.read_text(encoding="utf-8").splitlines()[0])
    assert row["capture_mode"] == "import"
    assert row["event_type"] == "historical-message"
    assert row["import_id"] == "bb_hist_20260331"


def test_normalize_bluebubbles_day_reads_imported_historical_messages(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = copy_export_fixture(tmp_path)
    import_bluebubbles_export(paths, export_path=export_dir, account="personal")

    event_path, artifact_path = normalize_bluebubbles_day(paths, date="2026-03-31")

    event_record = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    artifact_record = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])

    assert event_record["text"] == "Historical photo https://example.com/old-story"
    assert event_record["source_urls"] == ["https://example.com/old-story"]
    assert artifact_record["source_object_id"] == "at_hist_001"
    assert artifact_record["filename"] == "IMG_0999.jpeg"


def test_normalize_bluebubbles_day_preserves_existing_other_source_events(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = copy_export_fixture(tmp_path)
    import_bluebubbles_export(paths, export_path=export_dir, account="personal")
    write_normalized_events(
        paths,
        "2026-03-31",
        [
            NormalizedEvent(
                event_id="evt_slack_existing",
                source="slack",
                timestamp="2026-03-31T16:00:00Z",
                date="2026-03-31",
                kind="message",
                text="existing slack event",
            )
        ],
    )

    event_path, _ = normalize_bluebubbles_day(paths, date="2026-03-31")

    records = read_jsonl(event_path)
    assert {record["source"] for record in records} == {"slack", "bluebubbles"}
    assert any(record["event_id"] == "evt_slack_existing" for record in records)
    assert any(record["source"] == "bluebubbles" for record in records)


def test_export_bluebubbles_history_writes_bundle_from_messages_db(tmp_path) -> None:
    database_path = build_messages_db(tmp_path)
    output_dir = tmp_path / "export-output"

    result = export_bluebubbles_history(
        messages_db=database_path,
        output_dir=output_dir,
        from_date="2026-03-31",
        to_date="2026-03-31",
        export_id="bb_hist_20260331",
    )

    assert result.message_count == 1
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    message = json.loads(result.messages_path.read_text(encoding="utf-8").splitlines()[0])

    assert manifest["export_id"] == "bb_hist_20260331"
    assert manifest["message_count"] == 1
    assert message["guid"] == "p:0/8A5C4A20-3EFA-4AFB-9B41-8D0370F6D2D4"
    assert message["chatGuid"] == "iMessage;+15551234567"
    assert message["chatDisplayName"] == "Ariel"
    assert message["participants"] == ["+15551234567", "jon@icloud.com"]
    assert message["attachments"][0]["guid"] == "at_hist_001"
