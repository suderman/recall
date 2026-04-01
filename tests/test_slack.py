from __future__ import annotations

import json
import shutil
from pathlib import Path

from recall.connectors.slack.capture import capture_slack_day
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
                }
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
    assert result.stats["stored_messages"] == 3

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["account"] == "work"
    assert metadata["users"]["UPEER"] == "Ariel"

    message_lines = result.messages_path.read_text(encoding="utf-8").splitlines()
    assert len(message_lines) == 3


def test_normalize_slack_day_builds_daily_events(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    normalized_path = normalize_slack_day(paths, date="2026-03-31")

    assert normalized_path == tmp_path / "data" / "normalized" / "2026" / "2026-03-31.jsonl"

    records = [
        json.loads(line) for line in normalized_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 3

    first = records[0]
    assert first["conversation_label"] == "#webteam"
    assert first["sender_identity_id"] == "ident_slack_USELF"
    assert first["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]
    assert first["text"] == "Hey @Ariel review this (https://example.com)"
    assert first["links"] == ["https://example.com"]
    assert first["raw_ref"] == "data/raw/slack/2026-03-31/messages.jsonl:1"

    dm = records[2]
    assert dm["conversation_label"] == "DM:Ariel"
    assert dm["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]
