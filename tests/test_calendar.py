from __future__ import annotations

import json
from pathlib import Path

from recall.connectors.calendar.normalize import normalize_calendar_day
from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import read_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "calendar" / "events.json"


def _fixture_runner(arguments: list[str]) -> str:
    assert arguments[:2] == ["khal", "list"]
    assert "--json" in arguments
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_normalize_calendar_day_writes_events_with_expected_tags(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    event_path = normalize_calendar_day(paths, date="2026-03-31", runner=_fixture_runner)

    records = read_jsonl(event_path)
    assert len(records) == 5

    by_label = {record["conversation_label"]: record for record in records}
    team_sync = by_label["Team Sync"]
    assert team_sync["kind"] == "calendar_event"
    assert team_sync["sender_identity_id"] == "ident_email_manager_example_com"
    assert team_sync["participant_identity_ids"] == ["ident_email_manager_example_com"]
    assert team_sync["source_urls"] == ["https://meet.example.com/team"]
    assert "meeting" in team_sync["tags"]

    birthday = by_label["Birthday"]
    assert birthday["timestamp"] == "2026-03-31T00:00:00Z"
    assert "all_day" in birthday["tags"]
    assert "recurring" in birthday["tags"]
    assert birthday["thread_id"] == "uid-birthday"

    canceled = by_label["Canceled 1:1"]
    assert "canceled" in canceled["tags"]

    overnight = by_label["Overnight Trip"]
    assert "spans_days" in overnight["tags"]
    assert overnight["date"] == "2026-03-31"


def test_normalize_calendar_day_can_exclude_canceled_events(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    event_path = normalize_calendar_day(
        paths,
        date="2026-03-31",
        include_canceled=False,
        runner=_fixture_runner,
    )

    records = read_jsonl(event_path)
    labels = [record["conversation_label"] for record in records]
    assert "Canceled 1:1" not in labels


def test_normalize_calendar_day_is_replayable(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    first_path = normalize_calendar_day(paths, date="2026-03-31", runner=_fixture_runner)
    first_output = first_path.read_text(encoding="utf-8")
    second_path = normalize_calendar_day(paths, date="2026-03-31", runner=_fixture_runner)
    second_output = second_path.read_text(encoding="utf-8")

    assert first_path == second_path
    assert first_output == second_output


def test_normalize_calendar_day_merges_with_other_source_events(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_normalized_events(
        paths,
        "2026-03-31",
        [
            NormalizedEvent(
                event_id="evt_slack_1",
                source="slack",
                timestamp="2026-03-31T10:00:00Z",
                date="2026-03-31",
                kind="message",
                text="hello",
            )
        ],
    )

    event_path = normalize_calendar_day(paths, date="2026-03-31", runner=_fixture_runner)
    records = read_jsonl(event_path)

    assert any(record["source"] == "slack" for record in records)
    assert any(record["source"] == "calendar" for record in records)


def test_fixture_shape_is_valid_json() -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
