from __future__ import annotations

from functools import partial

from recall.normalize.rebuild import _preserve_input_downloads, rebuild_range
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths


def test_shared_artifacts_keep_links_and_input_downloads(tmp_path):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    raw = inputs.raw_capture_dir("bluebubbles", "2026-03-31") / "events.jsonl"
    write_jsonl(raw, [{
        "event_type": "new-message", "account": "personal",
        "received_at": "2026-03-31T12:00:00Z",
        "payload": {"guid": guid, "text": guid, "dateCreated": 1774958400000,
                    "attachments": [{"guid": "shared", "filename": "image.jpg"}]},
    } for guid in ["one", "two"]])
    run = partial(rebuild_range, inputs, output, first="2026-03-31",
                  last="2026-03-31", sources=["bluebubbles"])
    jobs = run()
    assert jobs[0]["status"] == "success"
    events = read_jsonl(output.normalized_event_path("2026-03-31"))
    assert len(events) == 2
    assert events[0]["artifact_ids"] == events[1]["artifact_ids"]
    artifacts = read_jsonl(output.artifact_metadata_path("bluebubbles", "2026-03-31"))
    assert len(artifacts) == 1
    assert set(artifacts[0]["event_ids"]) == {row["event_id"] for row in events}
    downloaded = artifacts[0]
    downloaded.update(download_status="downloaded", local_path="data/artifacts/blobs/image.jpg",
                      checksums={"sha256": "fixture"})
    blob = inputs.root / downloaded["local_path"]
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"fixture")
    write_jsonl(inputs.artifact_metadata_path("bluebubbles", "2026-03-31"), [downloaded])
    run()
    preserved = read_jsonl(output.artifact_metadata_path("bluebubbles", "2026-03-31"))[0]
    assert preserved["download_status"] == "downloaded"
    assert preserved["local_path"] == str(blob)
    assert preserved["checksums"] == {"sha256": "fixture"}
    observed = [{**downloaded, "download_status": status,
                 "local_path": "raw-copy", "checksums": {}}
                for status in ["not_requested", "imported", "downloaded"]]
    _preserve_input_downloads(inputs, "bluebubbles", observed)
    for row in observed:
        assert row["local_path"] == str(blob)
        assert row["checksums"] == {"sha256": "fixture"}
        assert row["download_status"] == "downloaded"
