from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

import recall.normalize.rebuild as replay
from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import read_jsonl, write_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths


def _workspaces(tmp_path):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    inputs.ensure_directories()
    return inputs, RecallPaths.from_root(tmp_path / "output")


def _telegram(inputs, capture_day="2026-04-02", timestamp="2026-04-01T05:59:59Z"):
    path = inputs.raw_capture_dir("telegram", capture_day) / "updates.jsonl"
    row = {"account": "personal", "received_at": "2026-04-02T12:00:00Z",
           "payload": {"message": {"id": 1, "chat_id": 42,
                                   "date": int(datetime.fromisoformat(timestamp).timestamp()),
                                   "content": {"@type": "messageText",
                                               "text": {"text": "Late arrival"}}}}}
    write_jsonl(path, [row, row])
    return path


def _run(inputs, output, sources=None, **options):
    return replay.rebuild_range(inputs, output, first="2026-03-31", last="2026-04-02",
                                sources=sources or ["telegram"],
                                timezone_name="America/Edmonton", **options)


def test_late_arrival_exact_dedupe_and_cached_resume(tmp_path, monkeypatch):
    inputs, output = _workspaces(tmp_path)
    raw = _telegram(inputs)
    original = raw.read_bytes()
    jobs = _run(inputs, output)
    rows = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-03-31"
    assert rows[0]["raw_ref"]["path"] == str(raw)
    assert [job["status"] for job in jobs] == ["success", "missing", "captured-empty"]
    assert jobs[0]["capture_day_present"] is False
    assert len({job["input_manifest"] for job in jobs}) == 1
    snapshot = read_jsonl(output.root / jobs[0]["input_manifest"])[0]
    assert snapshot["input_hashes"] == {str(raw): replay.file_hash(raw)}
    assert snapshot["fingerprint"] == jobs[0]["fingerprint"]
    assert all("input_hashes" not in job for job in jobs)
    before = output.normalized_event_path("2026-03-31").read_bytes()

    def unexpected(*args, **kwargs):
        pytest.fail("Unchanged raw source should resume from its checked cache")

    normalize, _, filename = replay.RAW_SOURCES["telegram"]
    monkeypatch.setitem(replay.RAW_SOURCES, "telegram", (normalize, unexpected, filename))
    assert _run(inputs, output) == jobs
    assert output.normalized_event_path("2026-03-31").read_bytes() == before
    assert raw.read_bytes() == original
    assert not inputs.database.exists()


def test_failure_and_missing_input_preserve_previous_output(tmp_path):
    inputs, output = _workspaces(tmp_path)
    raw = _telegram(inputs)
    _run(inputs, output)
    path = output.normalized_event_path("2026-03-31")
    before = path.read_bytes()
    raw.write_text("broken json\n")
    assert all(job["status"] == "failed" for job in _run(inputs, output))
    assert path.read_bytes() == before
    raw.parent.rename(raw.parent.with_name("not-a-date"))
    assert all(job["status"] == "missing" for job in _run(inputs, output))
    assert path.read_bytes() == before


def test_scoped_rebuild_and_proven_empty_preserve_other_accounts(tmp_path):
    inputs, output = _workspaces(tmp_path)
    _telegram(inputs, capture_day="2026-03-31")
    _run(inputs, output, account="personal")
    other = NormalizedEvent(event_id="other", source="telegram", account="work",
                            date="2026-03-31", timestamp="2026-03-31T12:00:00Z", kind="message")
    write_normalized_events(output, "2026-03-31", [other], merge_existing=True)
    write_jsonl(inputs.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl", [])
    jobs = _run(inputs, output, account="personal")
    assert jobs[0]["status"] == "captured-empty"
    remaining = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert [row["event_id"] for row in remaining] == ["other"]


def test_unsupported_is_not_empty(tmp_path):
    inputs, output = _workspaces(tmp_path)
    write_jsonl(inputs.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl",
                [{"payload": {"@type": "unknown"}}])
    assert _run(inputs, output)[0]["status"] == "unsupported"
    assert not output.normalized_event_path("2026-03-31").exists()


def test_reject_overlapping_roots_and_unknown_sources(tmp_path):
    inputs, output = _workspaces(tmp_path)
    with pytest.raises(ValueError, match="non-overlapping"):
        _run(inputs, inputs)
    with pytest.raises(ValueError, match="distinct sources"):
        _run(inputs, output, ["unknown"])
    with pytest.raises(ValueError, match="--to"):
        replay.date_range("2026-04-02", "2026-03-31")


def test_fixture_sources_together(tmp_path):
    inputs, output = _workspaces(tmp_path)
    fixtures = Path(__file__).parent / "fixtures"
    for source, filenames in {
        "slack": ["metadata.json", "conversations.json", "messages.jsonl"],
        "telegram": ["updates.jsonl"],
    }.items():
        target = inputs.raw_capture_dir(source, "2026-03-31")
        target.mkdir(parents=True)
        for filename in filenames:
            fixture_source = "slack_capture" if source == "slack" else source
            shutil.copy(fixtures / fixture_source / filename, target / filename)
    import json

    from recall.connectors.asana.importer import import_asana_export

    import_asana_export(inputs, export_path=fixtures / "asana", account="work")
    payload = json.loads((fixtures / "bluebubbles/new_message.json").read_text())
    write_jsonl(inputs.raw_capture_dir("bluebubbles", "2026-03-31") / "events.jsonl",
                [{"event_type": "new-message", "account": "personal", "payload": payload,
                  "received_at": "2026-03-31T12:00:00Z"}])
    jobs = _run(inputs, output, list(replay.RAW_SOURCES))
    assert not [job for job in jobs if job["status"] == "failed"]
    rows = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert {row["source"] for row in rows} == set(replay.RAW_SOURCES)
