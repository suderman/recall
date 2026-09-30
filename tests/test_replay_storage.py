from dataclasses import replace

import pytest

from recall.normalize.artifacts import NormalizedArtifact
from recall.normalize.events import NormalizedEvent
from recall.storage import jsonl
from recall.storage.paths import RecallPaths


def event(event_id="one", source="slack", account="work", **kwargs):
    return NormalizedEvent(
        event_id=event_id,
        source=source,
        account=account,
        timestamp="2026-03-31T12:00:00Z",
        date="2026-03-31",
        kind="message",
        **kwargs,
    )


def test_upsert_deduplicates_first_and_existing_batches(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    original = event(text="old")
    updated = replace(original, text="new")
    path = jsonl.write_normalized_events(
        paths, original.date, [original, updated], merge_existing=True
    )
    assert [row["text"] for row in jsonl.read_jsonl(path)] == ["new"]
    jsonl.write_jsonl(path, [original.to_record(), updated.to_record()])
    jsonl.write_normalized_events(paths, original.date, [], merge_existing=True)
    assert len(jsonl.read_jsonl(path)) == 1


def test_scoped_replace_removes_only_selected_source_account(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    path = jsonl.write_normalized_events(
        paths,
        "2026-03-31",
        [
            event(),
            event("personal", account="personal"),
            event("telegram", source="telegram"),
        ],
    )
    jsonl.write_normalized_events(paths, "2026-03-31", [], replace_scope=("slack", "work"))
    assert {row["event_id"] for row in jsonl.read_jsonl(path)} == {"personal", "telegram"}
    before = path.read_bytes()
    with pytest.raises(ValueError):
        jsonl.write_normalized_events(
            paths, "2026-03-31", [event(source="email")], replace_scope=("slack", "work")
        )
    assert path.read_bytes() == before


def test_failed_rewrite_preserves_bytes_and_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    jsonl.write_jsonl(path, [{"old": True}])
    before = path.read_bytes()
    with pytest.raises(TypeError):
        jsonl.write_jsonl(path, [{"new": True}, {"bad": object()}])
    assert path.read_bytes() == before

    def fail(*args):
        raise OSError("interrupted before replacement")

    monkeypatch.setattr(jsonl.os, "replace", fail)
    with pytest.raises(OSError):
        jsonl.write_jsonl(path, [{"new": True}])
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_artifact_replay_preserves_download_state_and_other_accounts(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    downloaded = NormalizedArtifact(
        artifact_id="a",
        source="slack",
        kind="file",
        account="work",
        local_path="blob",
        checksums={"sha256": "abc"},
        size_bytes=9,
        download_status="downloaded",
        last_error=None,
    )
    other = replace(downloaded, artifact_id="b", account="personal")
    path = jsonl.write_artifact_metadata(
        paths, source="slack", date="2026-03-31", artifacts=[downloaded, other]
    )
    fresh = NormalizedArtifact(artifact_id="a", source="slack", kind="file", account="work")
    jsonl.write_artifact_metadata(
        paths, source="slack", date="2026-03-31", artifacts=[fresh, fresh]
    )
    rows = {row["artifact_id"]: row for row in jsonl.read_jsonl(path)}
    assert set(rows) == {"a", "b"}
    for field in ("local_path", "checksums", "size_bytes", "download_status", "last_error"):
        assert rows["a"][field] == downloaded.to_record()[field]


def test_event_order_uses_instants_not_offset_strings(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    later = replace(event("later"), timestamp="2026-03-31T01:00:00-06:00")
    earlier = replace(event("earlier"), timestamp="2026-03-31T06:30:00Z")
    path = jsonl.write_normalized_events(paths, later.date, [later, earlier])
    assert [row["event_id"] for row in jsonl.read_jsonl(path)] == ["earlier", "later"]
