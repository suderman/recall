from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from recall.connectors.telegram.entities import sync_telegram_entities
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telegram"


def copy_fixture_capture(tmp_path: Path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "updates.jsonl", target_dir / "updates.jsonl")
    return paths


def test_normalize_telegram_day_writes_events_and_artifacts(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    event_path, artifact_path = normalize_telegram_day(paths, date="2026-03-31")

    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifact_records = [
        json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(records) == 2
    assert len(artifact_records) == 1

    first = records[0]
    assert first["source"] == "telegram"
    assert first["conversation_id"] == "1001"
    assert first["conversation_label"] == "Ariel"
    assert first["sender_identity_id"] == "ident_telegram_user_42"
    assert first["participant_identity_ids"] == [
        "ident_telegram_user_42",
        "ident_telegram_user_99",
    ]
    assert first["text"] == "Hey from Telegram https://example.com/story"
    assert first["source_urls"] == ["https://example.com/story"]
    assert first["raw_ref"] == {
        "source": "telegram",
        "path": "data/raw/telegram/2026-03-31/updates.jsonl",
        "locator": {"line": 1, "message_id": 9001, "chat_id": 1001},
    }
    assert first["tags"] == ["message", "telegram", "text"]

    second = records[1]
    assert second["sender_identity_id"] == "ident_telegram_user_99"
    assert second["source_urls"] == ["https://example.com/photo"]
    assert len(second["artifact_ids"]) == 1
    assert second["tags"] == ["message", "telegram", "photo"]

    artifact = artifact_records[0]
    assert artifact["source"] == "telegram"
    assert artifact["kind"] == "photo"
    assert artifact["source_object_id"] == "photo_001"
    assert artifact["filename"] == "IMG_1002.jpg"
    assert artifact["mime_type"] == "image/jpeg"
    assert artifact["size_bytes"] == 654321
    assert artifact["download_status"] == "not_requested"
    assert artifact["remote_locators"] == [
        {
            "kind": "local_path",
            "value": "/Users/jon/.local/share/telegram/photos/IMG_1002.jpg",
        },
        {"kind": "remote_id", "value": "telegram-file-001"},
        {"kind": "remote_unique_id", "value": "telegram-unique-001"},
    ]


def test_sync_telegram_entities_persists_users_chats_and_aliases(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    result = sync_telegram_entities(paths, date="2026-03-31")

    assert result.identities_synced == 3
    assert result.aliases_synced == 6

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
        ("telegram", "chat_id", "1001", None),
        ("telegram", "user_id", "42", None),
        ("telegram", "user_id", "99", None),
    ]
    assert len(aliases) == 6
    assert {value for _, value, _ in aliases} == {
        "+15551234567",
        "@ariel",
        "@jonsuderman",
        "Ariel",
        "Ariel Example",
        "Jon Suderman",
    }


def test_sync_telegram_entities_is_idempotent(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first = sync_telegram_entities(paths, date="2026-03-31")
    second = sync_telegram_entities(paths, date="2026-03-31")

    assert first.identities_synced == second.identities_synced == 3
    assert first.aliases_synced == second.aliases_synced == 6

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]

    assert identity_count == 3
    assert alias_count == 6
