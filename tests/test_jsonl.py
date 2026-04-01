from __future__ import annotations

import json

from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import write_normalized_events
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
        raw_ref="data/raw/slack/2026-03-31/messages.json",
    )

    destination = write_normalized_events(paths, event.date, [event])

    assert destination == tmp_path / "data" / "normalized" / "2026" / "2026-03-31.jsonl"
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt_slack_1"
