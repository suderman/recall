from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from recall.connectors.bluebubbles.capture import append_bluebubbles_event
from recall.connectors.bluebubbles.importer import import_bluebubbles_export
from recall.dedupe import inspect_overlaps
from recall.normalize.rebuild import rebuild_range
from recall.search import build_index, search
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths


@pytest.mark.parametrize("source", ["telegram", "bluebubbles"])
@pytest.mark.parametrize("repeat_day", ["2026-03-31", "2026-04-01"])
def test_repeated_native_records_publish_one_selected_event_and_count(
    tmp_path: Path, source: str, repeat_day: str
) -> None:
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    if source == "telegram":
        filename = "updates.jsonl"
        original = {
            "account": "personal",
            "capture_mode": "stream",
            "payload": {
                "message": {
                    "chat_id": 42,
                    "id": 1,
                    "date": 1774962000,
                    "content": {"@type": "messageText", "text": {"text": "Original"}},
                }
            },
        }
        # Same message number in another chat is a different native key.
        control = {
            **original,
            "payload": {"message": {**original["payload"]["message"], "chat_id": 43}},
        }
        latest = {
            **original,
            "payload": {
                "message": {
                    **original["payload"]["message"],
                    "content": {"@type": "messageText", "text": {"text": "Latest"}},
                }
            },
        }
        conversation = "42"
    else:
        filename = "events.jsonl"
        original = {
            "account": "personal",
            "capture_mode": "webhook",
            "event_type": "new-message",
            "received_at": "2026-03-31T13:00:00Z",
            "payload": {
                "data": {
                    "chatGuid": "chat-one",
                    "guid": "message-one",
                    "dateCreated": 1774962000000,
                    "text": "Original",
                }
            },
        }
        control = {
            **original,
            "payload": {"data": {**original["payload"]["data"], "chatGuid": "chat-two"}},
        }
        latest = {
            **original,
            "payload": {"data": {**original["payload"]["data"], "text": "Latest"}},
        }
        conversation = "chat-one"
    first = inputs.raw_capture_dir(source, "2026-03-31") / filename
    last = inputs.raw_capture_dir(source, repeat_day) / filename
    write_jsonl(first, [original, control])
    write_jsonl(last, [latest], append=True)
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in {first, last}}

    report = inspect_overlaps(inputs, sources=[source])
    assert len(report["groups"]) == 1
    assert report["groups"][0]["payload_variants"] == 2
    jobs = rebuild_range(inputs, output, first="2026-03-31", last="2026-03-31", sources=[source])
    assert jobs[0]["status"] == "success", jobs
    normalized = output.normalized_event_path("2026-03-31")
    stored = read_jsonl(normalized)
    assert len(stored) == 2
    selected = next(row for row in stored if row["conversation_id"] == conversation)
    assert selected["text"] == "Latest"
    assert selected["raw_ref"]["path"] == str(last)
    line = 3 if repeat_day == "2026-03-31" else 1
    assert selected["raw_ref"]["locator"]["line"] == line
    assert read_jsonl(last)[line - 1] == latest
    index = Path(build_index([output])["index"])
    results = search(index, "Latest")
    assert len(results) == 1
    assert results[0]["event"] == selected
    assert results[0]["normalized_path"] == str(normalized)
    assert read_jsonl(normalized)[results[0]["line"] - 1] == selected
    assert len(search(index)) == 2
    # Coverage counts published event IDs, not repeated capture-day observations.
    assert jobs[0]["event_count"] == len(stored)
    saved = normalized.read_bytes()
    assert (
        rebuild_range(inputs, output, first="2026-03-31", last="2026-03-31", sources=[source])
        == jobs
    )
    assert normalized.read_bytes() == saved
    build_index([output])
    assert search(index, "Latest") == results
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in {first, last}}
    assert after == before
    assert not inputs.database.exists()


def test_bluebubbles_live_import_and_late_receipt_replay_through_search(tmp_path: Path) -> None:
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    message = {
        "guid": "message-one",
        "chatGuid": "chat-one",
        "dateCreated": 1774962000000,
        "text": "Original",
    }
    append_bluebubbles_event(
        inputs,
        account="personal",
        payload={"type": "new-message", "data": message},
        received_at="2026-03-31T13:00:00Z",
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"export_id":"export-one"}', encoding="utf-8")
    write_jsonl(bundle / "messages.jsonl", [{**message, "text": "Imported"}])
    import_bluebubbles_export(inputs, export_path=bundle, account="personal")
    receipt = append_bluebubbles_event(
        inputs,
        account="personal",
        payload={"type": "new-message", "data": {**message, "text": "Latest"}},
        received_at="2026-04-01T13:00:00Z",
    )
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs.root.rglob("*")
        if path.is_file()
    }

    group = inspect_overlaps(inputs, sources=["bluebubbles"])["groups"][0]
    assert len(group["observations"]) == 3 and group["payload_variants"] == 3
    assert [row["capture_mode"] for row in group["observations"]] == [
        "webhook",
        "import",
        "webhook",
    ]
    jobs = rebuild_range(
        inputs, output, first="2026-03-31", last="2026-03-31", sources=["bluebubbles"]
    )
    assert jobs[0]["status"] == "success" and jobs[0]["event_count"] == 1
    stored = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert len(stored) == 1
    assert stored[0]["text"] == "Latest"
    assert stored[0]["raw_ref"]["path"] == str(receipt.events_path)
    assert stored[0]["raw_ref"]["locator"]["line"] == 1
    index = Path(build_index([output])["index"])
    assert search(index)[0]["event"] == stored[0]
    assert not search(index, "Imported") and not search(index, "Original")
    after = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs.root.rglob("*")
        if path.is_file()
    }
    assert after == before
