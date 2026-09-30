from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx

from recall.connectors.slack.artifacts import download_slack_artifacts
from recall.connectors.slack.capture import (
    SLACK_CURSOR_KEY,
    capture_slack_day,
    capture_slack_incremental,
)
from recall.connectors.slack.entities import sync_slack_entities
from recall.connectors.slack.normalize import normalize_slack_day
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor

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
            {
                "id": "USELF",
                "name": "jon",
                "real_name": "Jon",
                "profile": {"display_name": "Jon", "email": "jon@example.com"},
            },
            {
                "id": "UPEER",
                "name": "ariel",
                "real_name": "Ariel",
                "profile": {"display_name": "Ariel", "email": "ariel@example.com"},
            },
        ]

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, object]]:
        return [
            {"id": "C123", "name": "webteam", "is_archived": False, "is_private": False},
            {"id": "D456", "is_im": True, "user": "UPEER", "is_archived": False},
        ]

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str, inclusive: bool = True
    ) -> list[dict[str, object]]:
        del oldest, latest, inclusive
        if channel_id == "C123":
            return [
                {
                    "files": [
                        {
                            "id": "F123",
                            "mimetype": "image/png",
                            "name": "diagram.png",
                            "permalink": "https://workspace.slack.com/files/USELF/F123/diagram.png",
                            "size": 482193,
                            "url_private": "https://files.slack.com/files-pri/T123-F123/diagram.png",
                            "url_private_download": "https://files.slack.com/files-pri/T123-F123/download/diagram.png",
                        }
                    ],
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


class IncrementalSlackClient:
    def __init__(self) -> None:
        self.history = {
            "C123": [
                {
                    "files": [
                        {
                            "id": "F123",
                            "mimetype": "image/png",
                            "name": "diagram.png",
                            "permalink": "https://workspace.slack.com/files/USELF/F123/diagram.png",
                            "size": 482193,
                            "url_private": "https://files.slack.com/files-pri/T123-F123/diagram.png",
                            "url_private_download": "https://files.slack.com/files-pri/T123-F123/download/diagram.png",
                        }
                    ],
                    "ts": "1774976467.000100",
                    "user": "USELF",
                    "text": "Hey <@UPEER> review <https://example.com|this>",
                    "reply_count": 1,
                },
                {
                    "ts": "1775062867.000100",
                    "user": "USELF",
                    "text": "Daily follow-up",
                },
            ],
            "D456": [
                {"ts": "1774980000.000300", "user": "UPEER", "text": "Lunch?"},
                {"ts": "1775066400.000300", "user": "UPEER", "text": "Tomorrow works"},
            ],
        }
        self.replies = {
            ("C123", "1774976467.000100"): [
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
        }

    def auth_test(self) -> dict[str, str]:
        return {
            "team": "Example Workspace",
            "team_id": "T123",
            "user": "jon",
            "user_id": "USELF",
        }

    def list_users(self) -> list[dict[str, object]]:
        return [
            {
                "id": "USELF",
                "name": "jon",
                "real_name": "Jon",
                "profile": {"display_name": "Jon", "email": "jon@example.com"},
            },
            {
                "id": "UPEER",
                "name": "ariel",
                "real_name": "Ariel",
                "profile": {"display_name": "Ariel", "email": "ariel@example.com"},
            },
        ]

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, object]]:
        del include_archived
        return [
            {"id": "C123", "name": "webteam", "is_archived": False, "is_private": False},
            {"id": "D456", "is_im": True, "user": "UPEER", "is_archived": False},
        ]

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str, inclusive: bool = True
    ) -> list[dict[str, object]]:
        lower = Decimal(oldest)
        upper = Decimal(latest)
        rows: list[dict[str, object]] = []
        for message in self.history.get(channel_id, []):
            ts = Decimal(str(message["ts"]))
            is_after = ts >= lower if inclusive else ts > lower
            if is_after and ts <= upper:
                rows.append(dict(message))
        return rows

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, object]]:
        return [dict(row) for row in self.replies.get((channel_id, ts), [])]


class FakeArtifactStreamClient:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        assert headers["Authorization"].startswith("Bearer ")
        request = httpx.Request("GET", url)
        if url not in self.payloads:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, content=self.payloads[url])

    def close(self) -> None:
        return None


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
    assert metadata["user_profiles"]["UPEER"]["email"] == "ariel@example.com"

    message_lines = result.messages_path.read_text(encoding="utf-8").splitlines()
    assert len(message_lines) == 4


def test_normalize_slack_day_builds_daily_events(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    normalized_path = normalize_slack_day(paths, date="2026-03-31")
    artifact_path = paths.artifact_metadata_path("slack", "2026-03-31")

    assert normalized_path == tmp_path / "data" / "normalized" / "2026" / "2026-03-31.jsonl"
    assert (
        artifact_path
        == tmp_path / "data" / "artifacts" / "metadata" / "slack" / "2026" / "2026-03-31.jsonl"
    )

    records = [
        json.loads(line) for line in normalized_path.read_text(encoding="utf-8").splitlines()
    ]
    artifact_records = [
        json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 4
    assert len(artifact_records) == 1

    first = records[0]
    assert first["conversation_label"] == "#webteam"
    assert first["sender_identity_id"] == "ident_slack_USELF"
    assert first["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]
    assert first["text"] == "Hey <@UPEER> review <https://example.com|this>"
    assert first["source_urls"] == ["https://example.com"]
    assert len(first["artifact_ids"]) == 1
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

    artifact = artifact_records[0]
    assert artifact["artifact_id"] == first["artifact_ids"][0]
    assert artifact["source_object_id"] == "F123"
    assert artifact["filename"] == "diagram.png"
    assert artifact["mime_type"] == "image/png"
    assert artifact["size_bytes"] == 482193
    assert artifact["download_status"] == "not_requested"
    assert artifact["event_ids"] == [first["event_id"]]
    assert artifact["remote_locators"] == [
        {
            "kind": "url_private",
            "value": "https://files.slack.com/files-pri/T123-F123/diagram.png",
        },
        {
            "kind": "url_private_download",
            "value": "https://files.slack.com/files-pri/T123-F123/download/diagram.png",
        },
        {
            "kind": "permalink",
            "value": "https://workspace.slack.com/files/USELF/F123/diagram.png",
        },
    ]
    assert artifact["raw_ref"] == {
        "source": "slack",
        "path": "data/raw/slack/2026-03-31/messages.jsonl",
        "locator": {
            "channel": "C123",
            "ts": "1774976467.000100",
            "file_id": "F123",
        },
    }

    bot_message = next(row for row in records
                       if row["sender_identity_id"] == "ident_slack_bot_BHELPER")
    assert bot_message["sender_identity_id"] == "ident_slack_bot_BHELPER"
    assert bot_message["source_urls"] == []
    assert bot_message["artifact_ids"] == []

    dm = next(row for row in records if row["conversation_label"] == "DM:Ariel")
    assert dm["conversation_label"] == "DM:Ariel"
    assert dm["participant_identity_ids"] == ["ident_slack_UPEER", "ident_slack_USELF"]


def test_sync_slack_entities_persists_identities_and_aliases(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    result = sync_slack_entities(paths, date="2026-03-31")

    assert result.identities_synced == 5
    assert result.aliases_synced == 6

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
        ("ident_slack_email_ariel_at_example_com", "email", "ariel@example.com", None),
        ("ident_slack_email_jon_at_example_com", "email", "jon@example.com", None),
    ]
    assert aliases == [
        ("ident_slack_UPEER", "Ariel", "slack_user_profile"),
        (
            "ident_slack_UPEER",
            "ariel@example.com",
            "slack_user_email",
        ),
        ("ident_slack_USELF", "Jon", "slack_user_profile"),
        (
            "ident_slack_USELF",
            "jon@example.com",
            "slack_user_email",
        ),
        (
            "ident_slack_email_ariel_at_example_com",
            "ariel@example.com",
            "slack_user_email",
        ),
        (
            "ident_slack_email_jon_at_example_com",
            "jon@example.com",
            "slack_user_email",
        ),
    ]


def test_sync_slack_entities_is_idempotent(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first = sync_slack_entities(paths, date="2026-03-31")
    second = sync_slack_entities(paths, date="2026-03-31")

    assert first.identities_synced == second.identities_synced == 5
    assert first.aliases_synced == second.aliases_synced == 6

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]

    assert identity_count == 5
    assert alias_count == 6


def test_normalize_slack_day_is_replayable_from_same_raw_capture(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first_path = normalize_slack_day(paths, date="2026-03-31")
    first_output = first_path.read_text(encoding="utf-8")
    second_path = normalize_slack_day(paths, date="2026-03-31")
    second_output = second_path.read_text(encoding="utf-8")

    assert first_path == second_path
    assert first_output == second_output


def test_download_slack_artifacts_respects_metadata_only_policy(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)
    normalize_slack_day(paths, date="2026-03-31")

    result = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="metadata-only",
        client=FakeArtifactStreamClient({}),
    )

    assert result.artifacts_seen == 1
    assert result.would_download == 0
    assert result.downloaded == 0
    assert result.skipped_policy == 1

    artifact_path = paths.artifact_metadata_path("slack", "2026-03-31")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert artifact["download_status"] == "not_requested"
    assert artifact["local_path"] is None
    assert artifact["checksums"] == {}
    assert artifact["last_error"] is None


def test_download_slack_artifacts_dry_run_leaves_metadata_unchanged(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)
    normalize_slack_day(paths, date="2026-03-31")
    client = FakeArtifactStreamClient(
        {"https://files.slack.com/files-pri/T123-F123/download/diagram.png": b"png-bytes"}
    )
    artifact_path = paths.artifact_metadata_path("slack", "2026-03-31")
    before = artifact_path.read_text(encoding="utf-8")

    result = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="download-source-native",
        client=client,
        dry_run=True,
    )

    assert result.artifacts_seen == 1
    assert result.would_download == 1
    assert result.downloaded == 0
    assert result.failed == 0
    assert artifact_path.read_text(encoding="utf-8") == before


def test_download_slack_artifacts_downloads_source_native_file(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)
    normalize_slack_day(paths, date="2026-03-31")
    client = FakeArtifactStreamClient(
        {"https://files.slack.com/files-pri/T123-F123/download/diagram.png": b"png-bytes"}
    )

    result = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="download-source-native",
        client=client,
    )

    assert result.downloaded == 1
    assert result.would_download == 1
    assert result.failed == 0

    artifact_path = paths.artifact_metadata_path("slack", "2026-03-31")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert artifact["download_status"] == "downloaded"
    assert artifact["local_path"] is not None
    assert artifact["checksums"]["sha256"]
    assert artifact["last_error"] is None

    blob_path = paths.root / artifact["local_path"]
    assert blob_path.exists()
    assert blob_path.read_bytes() == b"png-bytes"


def test_download_slack_artifacts_safe_rerun_skips_existing_blob(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)
    normalize_slack_day(paths, date="2026-03-31")
    client = FakeArtifactStreamClient(
        {"https://files.slack.com/files-pri/T123-F123/download/diagram.png": b"png-bytes"}
    )

    first = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="download-source-native",
        client=client,
    )
    second = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="download-source-native",
        client=client,
    )

    assert first.downloaded == 1
    assert second.downloaded == 0
    assert second.would_download == 0
    assert second.skipped_existing == 1


def test_download_slack_artifacts_records_failure_detail(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)
    normalize_slack_day(paths, date="2026-03-31")
    client = FakeArtifactStreamClient({})

    result = download_slack_artifacts(
        paths,
        date="2026-03-31",
        token="xoxp-test",
        policy="download-source-native",
        client=client,
    )

    assert result.downloaded == 0
    assert result.failed == 1
    assert result.would_download == 1

    artifact_path = paths.artifact_metadata_path("slack", "2026-03-31")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert artifact["download_status"] == "failed"
    assert "404 Not Found" in artifact["last_error"]
    assert "T123-F123/download/diagram.png" in artifact["last_error"]


def test_incremental_capture_without_cursor_uses_explicit_since(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    result = capture_slack_incremental(
        paths,
        client=IncrementalSlackClient(),
        account="work",
        cursor_before=None,
        since="2026-03-31T00:00:00Z",
        until="2026-03-31T23:59:59Z",
    )

    assert result.cursor_before is None
    assert result.cursor_after == "1774980000.000300"
    assert result.dates_written == ["2026-03-31"]

    cursor = get_connector_cursor(
        paths,
        source="slack",
        account="work",
        cursor_key=SLACK_CURSOR_KEY,
    )
    assert cursor is not None
    assert cursor.cursor_value == "1774980000.000300"

    raw_dir = paths.raw_capture_dir("slack", "2026-03-31")
    message_count = len((raw_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines())
    assert message_count == 3


def test_incremental_capture_with_existing_cursor_appends_newer_messages(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    client = IncrementalSlackClient()

    first = capture_slack_incremental(
        paths,
        client=client,
        account="work",
        cursor_before=None,
        since="2026-03-31T00:00:00Z",
        until="2026-03-31T23:59:59Z",
    )
    cursor = get_connector_cursor(
        paths,
        source="slack",
        account="work",
        cursor_key=SLACK_CURSOR_KEY,
    )

    second = capture_slack_incremental(
        paths,
        client=client,
        account="work",
        cursor_before=cursor,
        since=None,
        until="2026-04-01T23:59:59Z",
    )

    assert first.cursor_after == "1774980000.000300"
    assert second.cursor_before == "1774980000.000300"
    assert second.cursor_after == "1775066400.000300"
    assert second.dates_written == ["2026-04-01"]

    second_day_dir = paths.raw_capture_dir("slack", "2026-04-01")
    message_count = len(
        (second_day_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert message_count == 2


def test_incremental_capture_safe_rerun_does_not_duplicate_messages(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    client = IncrementalSlackClient()

    capture_slack_incremental(
        paths,
        client=client,
        account="work",
        cursor_before=None,
        since="2026-03-31T00:00:00Z",
        until="2026-03-31T23:59:59Z",
    )
    cursor = get_connector_cursor(
        paths,
        source="slack",
        account="work",
        cursor_key=SLACK_CURSOR_KEY,
    )
    rerun = capture_slack_incremental(
        paths,
        client=client,
        account="work",
        cursor_before=cursor,
        since=None,
        until="2026-03-31T23:59:59Z",
    )

    assert rerun.stored_messages == 0
    assert rerun.cursor_updated is False
    assert rerun.dates_written == []

    day_dir = paths.raw_capture_dir("slack", "2026-03-31")
    message_count = len((day_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines())
    assert message_count == 3


def test_day_bounded_capture_does_not_require_or_update_cursor_state(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    capture_slack_day(
        paths,
        client=FakeSlackClient(),
        date="2026-03-31",
        account="work",
    )

    cursor = get_connector_cursor(
        paths,
        source="slack",
        account="work",
        cursor_key=SLACK_CURSOR_KEY,
    )
    assert cursor is None
