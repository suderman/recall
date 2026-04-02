from __future__ import annotations

import json

from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import read_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths


def test_write_normalized_events_uses_daily_jsonl_partition(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    event = NormalizedEvent(
        event_id="evt_slack_1",
        source="slack",
        timestamp="2026-03-31T17:31:07Z",
        date="2026-03-31",
        kind="message",
        text="hello world",
        raw_ref=RawReference(
            source="slack",
            path="data/raw/slack/2026-03-31/messages.jsonl",
            locator={"channel": "C123", "ts": "1774976467.000100"},
        ),
    )

    destination = write_normalized_events(paths, event.date, [event])

    assert destination == tmp_path / "data" / "normalized" / "2026" / "2026-03-31.jsonl"
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_id"] == "evt_slack_1"
    assert record["raw_ref"]["locator"]["channel"] == "C123"


def test_write_normalized_events_can_merge_existing_sources(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    slack_event = NormalizedEvent(
        event_id="evt_slack_1",
        source="slack",
        timestamp="2026-03-31T17:31:07Z",
        date="2026-03-31",
        kind="message",
        text="slack event",
    )
    bluebubbles_event = NormalizedEvent(
        event_id="evt_bluebubbles_1",
        source="bluebubbles",
        timestamp="2026-03-31T18:31:07Z",
        date="2026-03-31",
        kind="message",
        text="bluebubbles event",
    )

    write_normalized_events(paths, slack_event.date, [slack_event])
    destination = write_normalized_events(
        paths,
        bluebubbles_event.date,
        [bluebubbles_event],
        merge_existing=True,
    )

    records = read_jsonl(destination)
    assert [record["event_id"] for record in records] == ["evt_slack_1", "evt_bluebubbles_1"]


def test_write_normalized_events_merge_replaces_same_event_id(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    original = NormalizedEvent(
        event_id="evt_slack_1",
        source="slack",
        timestamp="2026-03-31T17:31:07Z",
        date="2026-03-31",
        kind="message",
        text="old text",
    )
    updated = NormalizedEvent(
        event_id="evt_slack_1",
        source="slack",
        timestamp="2026-03-31T17:31:07Z",
        date="2026-03-31",
        kind="message",
        text="new text",
    )

    write_normalized_events(paths, original.date, [original])
    destination = write_normalized_events(paths, updated.date, [updated], merge_existing=True)

    records = read_jsonl(destination)
    assert len(records) == 1
    assert records[0]["text"] == "new text"
