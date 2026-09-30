from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

import recall.normalize.rebuild as replay
from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import read_jsonl, write_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths


def test_local_queries_and_failure_do_not_erase_prior_evidence(tmp_path, monkeypatch):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    fixtures = Path(__file__).parent / "fixtures"
    import recall.connectors.calendar.khal as calendar
    import recall.connectors.email.notmuch as email

    monkeypatch.setattr(email, "run_notmuch_command", lambda _: "\n".join(
        str(path) for path in sorted((fixtures / "email/messages").glob("*.eml"))))
    monkeypatch.setattr(calendar, "run_khal_command", lambda _, **kwargs: json.dumps([{
        "uid": "meeting", "title": "Canceled meeting", "start": "2026-03-31T18:00:00Z",
        "end": "2026-03-31T19:00:00Z", "calendar": "work", "status": "CANCELLED",
    }]))
    run = partial(replay.rebuild_range, inputs, output, first="2026-03-31",
                  last="2026-03-31", sources=["email", "calendar"])
    jobs = run()
    assert [job["status"] for job in jobs] == ["success", "success"], [j["error"] for j in jobs]
    assert all(job["query_snapshot_hash"] for job in jobs)
    before = output.normalized_event_path("2026-03-31").read_bytes()

    def failure(_, **kwargs):
        raise RuntimeError("query unavailable")

    monkeypatch.setattr(email, "run_notmuch_command", failure)
    monkeypatch.setattr(calendar, "run_khal_command", failure)
    jobs = run()
    assert [job["status"] for job in jobs] == ["failed", "failed"]
    assert output.normalized_event_path("2026-03-31").read_bytes() == before
    monkeypatch.setattr(email, "run_notmuch_command", lambda _: "")
    jobs = run()
    assert jobs[0]["status"] == "queried-empty"
    remaining = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert {row["source"] for row in remaining} == {"calendar"}
    assert not inputs.root.exists()


def test_interrupted_publication_resumes_without_resetting_input(tmp_path, monkeypatch):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    raw = inputs.raw_capture_dir("asana", "2026-03-31") / "events.jsonl"
    write_jsonl(raw, [{"event_type": "task", "account": "work",
                       "received_at": "2026-03-31T12:00:00Z",
                       "payload": {"gid": "1", "name": "Task"}}])
    run = partial(replay.rebuild_range, inputs, output, first="2026-03-31",
                  last="2026-04-01", sources=["asana"])
    publish = replay._publish_day

    def interrupt(output, source, day, *args):
        if day == "2026-04-01":
            raise KeyboardInterrupt()
        publish(output, source, day, *args)

    # Both days have input, so the second publication is reached.
    write_jsonl(inputs.raw_capture_dir("asana", "2026-04-01") / "events.jsonl", [])
    monkeypatch.setattr(replay, "_publish_day", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert len(read_jsonl(output.normalized_event_path("2026-03-31"))) == 1
    manifest = output.state / "rebuild/manifest.jsonl"
    assert len(read_jsonl(manifest)) == 1
    monkeypatch.setattr(replay, "_publish_day", publish)
    assert [job["status"] for job in run()] == [
        "success", "captured-empty"]
    assert len(read_jsonl(manifest)) == 2
    assert raw.exists()


def test_failed_publication_keeps_normalized_file_intact(tmp_path, monkeypatch):
    output = RecallPaths.from_root(tmp_path)
    event = NormalizedEvent(event_id="old", source="email", account="default", kind="email",
                            date="2026-03-31", timestamp="2026-03-31T12:00:00Z")
    write_normalized_events(output, event.date, [event])
    before = output.normalized_event_path(event.date).read_bytes()
    import recall.storage.jsonl as storage

    def fail(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError):
        replay._publish_day(output, "email", event.date, [], [], "default")
    assert output.normalized_event_path(event.date).read_bytes() == before
