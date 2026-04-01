from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from recall.connectors.slack.capture import capture_slack_day
from recall.connectors.slack.entities import sync_slack_entities
from recall.connectors.slack.normalize import normalize_slack_day
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "slack_capture"


class FakeSlackClient:
    def auth_test(self) -> dict[str, str]:
        return {
            "team": "Example Workspace",
            "team_id": "T123",
            "user": "jon",
            "user_id": "USELF",
        }

    def list_users(self) -> list[dict[str, object]]:
        return [
            {"id": "USELF", "name": "jon", "real_name": "Jon", "profile": {"display_name": "Jon"}},
            {
                "id": "UPEER",
                "name": "ariel",
                "real_name": "Ariel",
                "profile": {"display_name": "Ariel"},
            },
        ]

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, object]]:
        return [
            {"id": "C123", "name": "webteam", "is_archived": False, "is_private": False},
            {"id": "D456", "is_im": True, "user": "UPEER", "is_archived": False},
        ]

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str
    ) -> list[dict[str, object]]:
        del oldest, latest
        if channel_id == "C123":
            return [
                {
                    "ts": "1774976467.000100",
                    "user": "USELF",
                    "text": "Hey <@UPEER> review <https://example.com|this>",
                    "reply_count": 1,
                },
                {
                    "ts": "1774977000.000150",
                    "bot_id": "BHELPER",
                    "username": "Helper Bot",
                    "text": "Reminder sent",
                },
            ]
        if channel_id == "D456":
            return [{"ts": "1774980000.000300", "user": "UPEER", "text": "Lunch?"}]
        raise AssertionError(f"Unexpected channel: {channel_id}")

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, object]]:
        if channel_id == "C123" and ts == "1774976467.000100":
            return [
                {
                    "ts": "1774976467.000100",
                    "user": "USELF",
                    "text": "Hey <@UPEER> review <https://example.com|this>",
                },
                {
                    "ts": "1774977467.000200",
                    "thread_ts": "1774976467.000100",
                    "user": "UPEER",
                    "text": "Looks good",
                },
            ]
        raise AssertionError(f"Unexpected thread lookup: {channel_id} {ts}")


def copy_fixture_capture(tmp_path: Path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("slack", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in ("metadata.json", "conversations.json", "messages.jsonl"):
        shutil.copy(FIXTURE_DIR / name, target_dir / name)

    return paths


def test_capture_slack_day_writes_expected_raw_files(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    result = capture_slack_day(
        paths,
        client=FakeSlackClient(),
        date="2026-03-31",
        account="work",
    )

    assert result.metadata_path.exists()
    assert result.conversations_path.exists()
    assert result.messages_path.exists()
    assert result.stats["stored_messages"] == 4

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["account"] == "work"
    assert metadata["users"]["UPEER"] == "Ariel"

    message_lines = result.messages_path.read_text(encoding="utf-8").splitlines()
    assert len(message_lines) == 4


def test_normalize_slack_day_builds_daily_events(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    normalized_path = normalize_slack_day(paths, date="2026-03-31")

    assert normalized_path == tmp_path / "data" / "normalized" / "2026" / "2026-03-31.jsonl"

    records = [
        json.loads(line) for line in normalized_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 4

    first = records[0]
    assert first["conversation_label"] == "#webteam"
    assert first["sender_identity_id"] == "ident_slack_USELF"
    assert first["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]
    assert first["text"] == "Hey @Ariel review this (https://example.com)"
    assert first["url"] == "https://example.com"
    assert first["raw_ref"] == {
        "source": "slack",
        "path": "data/raw/slack/2026-03-31/messages.jsonl",
        "locator": {
            "channel": "C123",
            "ts": "1774976467.000100",
        },
    }
    assert first["raw_fragment"] is None
    assert first["tags"] == ["message"]

    bot_message = records[2]
    assert bot_message["sender_identity_id"] == "ident_slack_bot_BHELPER"
    assert bot_message["url"] is None

    dm = records[3]
    assert dm["conversation_label"] == "DM:Ariel"
    assert dm["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]


def test_sync_slack_entities_persists_identities_and_aliases(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    result = sync_slack_entities(paths, date="2026-03-31")

    assert result.identities_synced == 3
    assert result.aliases_synced == 2

    with sqlite3.connect(paths.database) as connection:
        identities = connection.execute(
            "select identity_id, kind, value, person_id from identities order by identity_id"
        ).fetchall()
        aliases = connection.execute(
            "select identity_id, value, source from identity_aliases order by identity_id, value"
        ).fetchall()

    assert identities == [
        ("ident_slack_UPEER", "user_id", "UPEER", None),
        ("ident_slack_USELF", "user_id", "USELF", None),
        ("ident_slack_bot_BHELPER", "bot_id", "BHELPER", None),
    ]
    assert aliases == [
        ("ident_slack_UPEER", "Ariel", "slack_user_profile"),
        ("ident_slack_USELF", "Jon", "slack_user_profile"),
    ]


def test_sync_slack_entities_is_idempotent(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first = sync_slack_entities(paths, date="2026-03-31")
    second = sync_slack_entities(paths, date="2026-03-31")

    assert first.identities_synced == second.identities_synced == 3
    assert first.aliases_synced == second.aliases_synced == 2

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]

    assert identity_count == 3
    assert alias_count == 2


def test_normalize_slack_day_is_replayable_from_same_raw_capture(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first_path = normalize_slack_day(paths, date="2026-03-31")
    first_output = first_path.read_text(encoding="utf-8")
    second_path = normalize_slack_day(paths, date="2026-03-31")
    second_output = second_path.read_text(encoding="utf-8")

    assert first_path == second_path
    assert first_output == second_output
