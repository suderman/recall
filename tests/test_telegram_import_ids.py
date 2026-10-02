from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path

import pytest

from recall.connectors.telegram.capture import append_telegram_update
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.dedupe import inspect_overlaps
from recall.normalize.rebuild import rebuild_range
from recall.search import build_index, search
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize.journal import _load_packet, prepare_journal

DAY = "2026-03-31"


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".lock"
    }


def digest(value: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def document_row(**fields) -> dict:
    return {
        "account": "personal",
        "capture_mode": "stream",
        "received_at": "2026-03-31T21:00:00Z",
        "payload": {
            "message": {
                "chat_id": 5,
                "id": 1048576,
                "date": 1774990800,
                "content": {
                    "@type": "messageDocument",
                    "caption": {"text": "Native fixture"},
                    "document": {"file_name": "native.bin", "document": {"id": 991}},
                },
            }
        },
        **fields,
    }


def seed_archive(root: Path) -> RecallPaths:
    paths = RecallPaths.from_root(root / "inputs")
    append_telegram_update(
        paths,
        account="personal",
        payload=document_row()["payload"],
        update_type="updateNewMessage",
        capture_mode="stream",
        received_at="2026-03-31T21:00:00Z",
    )
    for number in (1, 2):
        bundle = root / f"export-{number}"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files/document.bin").write_bytes(f"Fixture bytes {number}".encode())
        (bundle / "result.json").write_text(
            json.dumps(
                {
                    "chats": {
                        "list": [
                            {
                                "id": 5,
                                "name": "Fixture",
                                "messages": [
                                    {
                                        "type": "message",
                                        "id": 1048576,
                                        "date": "2026-03-31T21:00:00Z",
                                        "date_unixtime": "1774990800",
                                        "text": f"Export fixture {number}",
                                        "file": "files/document.bin",
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        )
        import_telegram_export(paths, export_path=bundle, account="personal")
    return paths


def test_import_ids_separate_actual_bundles_and_native_media(tmp_path) -> None:
    paths = seed_archive(tmp_path)
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    inputs = snapshot(paths.raw)
    event_path, artifact_path = normalize_telegram_day(paths, date=DAY)
    events, artifacts = read_jsonl(event_path), read_jsonl(artifact_path)
    assert len(events) == len(artifacts) == 3
    by_line = {e["raw_ref"]["locator"]["line"]: e for e in events}
    media_by_id = {a["artifact_id"]: a for a in artifacts}
    for line, envelope in enumerate(read_jsonl(raw), 1):
        event = by_line[line]
        artifact = media_by_id[event["artifact_ids"][0]]
        if line == 1:
            expected_event = (
                "evt_" + hashlib.sha256(b"telegram:personal:5:1048576").hexdigest()[:20]
            )
            expected_artifact = (
                "artifact_"
                + hashlib.sha256(b"telegram:personal:artifact:1048576:991").hexdigest()[:20]
            )
        else:
            expected_event = "evt_" + digest(
                ["telegram-export-v1", "personal", envelope["import_id"], "5", "1048576"]
            )
            expected_artifact = "artifact_" + digest(
                ["telegram-export-artifact-v1", expected_event, artifact["source_object_id"]]
            )
        assert event["event_id"] == expected_event
        assert artifact["artifact_id"] == expected_artifact
        assert artifact["event_ids"] == [expected_event]
        assert artifact["raw_ref"]["locator"]["line"] == line
        assert artifact["raw_ref"]["path"] == event["raw_ref"]["path"]
        assert artifact["download_status"] == "not_requested" and artifact["checksums"] == {}
    exported = [a for a in artifacts if a["raw_ref"]["locator"]["line"] != 1]
    assert exported[0]["source_object_id"] == exported[1]["source_object_id"]
    files = [Path(a["remote_locators"][0]["value"]) for a in exported]
    assert files[0] != files[1] and files[0].read_bytes() != files[1].read_bytes()
    assert inspect_overlaps(paths, sources=["telegram"])["normalization_collisions"] == []
    before = (event_path.read_bytes(), artifact_path.read_bytes())
    normalize_telegram_day(paths, date=DAY)
    assert (event_path.read_bytes(), artifact_path.read_bytes()) == before
    assert snapshot(paths.raw) == inputs


@pytest.mark.parametrize("invalid", [None, "", " \t", 1, False])
@pytest.mark.parametrize("message_present", [True, False])
def test_bad_namespace_rejects_before_publication(tmp_path, invalid, message_present) -> None:
    paths = RecallPaths.from_root(tmp_path)
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    write_jsonl(raw, [document_row()])
    event_path, artifact_path = normalize_telegram_day(paths, date=DAY)
    bad = document_row(capture_mode="import", import_id=invalid)
    if not message_present:
        bad["payload"] = {"@type": "updateUser", "id": 1}
    write_jsonl(raw, [document_row(), bad])
    before = snapshot(paths.root)
    with pytest.raises(ValueError, match="import_id") as error:
        normalize_telegram_day(paths, date=DAY)
    assert str(error.value).endswith(f"{raw}:2")
    assert snapshot(paths.root) == before
    assert len(read_jsonl(event_path)) == len(read_jsonl(artifact_path)) == 1


def test_import_key_parts_and_retained_namespace(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    rows = []
    for account, bundle, chat, message in [
        ("a:b", "c", 5, 6),
        ("a", "b:c", 5, 6),
        ("a:b", "c", 7, 6),
        ("a:b", "c", 5, 8),
        ("a:b", " c ", 5, 6),
        ("a:b", "résumé", 5, 6),
    ]:
        row = document_row(account=account, capture_mode="import", import_id=bundle)
        row["payload"]["message"].update(chat_id=chat, id=message)
        rows.append(row)
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    repeated = document_row(account="a:b", capture_mode="import", import_id="c")
    repeated["payload"]["message"]["id"] = 6
    repeated["payload"]["message"]["content"]["caption"]["text"] = "Latest fixture"
    rows.append(repeated)
    write_jsonl(raw, rows)
    event_path, artifact_path = normalize_telegram_day(paths, date=DAY)
    events, artifacts = read_jsonl(event_path), read_jsonl(artifact_path)
    assert len(events) == len(artifacts) == 6
    selected = next(e for e in events if e["text"] == "Latest fixture")
    assert selected["raw_ref"]["locator"]["line"] == 7
    for event in events:
        row = rows[event["raw_ref"]["locator"]["line"] - 1]
        message = row["payload"]["message"]
        assert event["event_id"] == "evt_" + digest(
            [
                "telegram-export-v1",
                row["account"],
                row["import_id"],
                str(message["chat_id"]),
                str(message["id"]),
            ]
        )
    before = snapshot(paths.root)
    normalize_telegram_day(paths, date=DAY)
    assert snapshot(paths.root) == before


def test_native_capture_ignores_unrelated_import_id(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_jsonl(
        paths.raw_capture_dir("telegram", DAY) / "updates.jsonl",
        [document_row(import_id="not-a-native-namespace")],
    )
    event_path, artifact_path = normalize_telegram_day(paths, date=DAY)
    assert read_jsonl(event_path)[0]["event_id"] == (
        "evt_" + hashlib.sha256(b"telegram:personal:5:1048576").hexdigest()[:20]
    )
    assert read_jsonl(artifact_path)[0]["artifact_id"] == (
        "artifact_" + hashlib.sha256(b"telegram:personal:artifact:1048576:991").hexdigest()[:20]
    )


def test_private_replay_does_not_inherit_legacy_import_bytes(tmp_path) -> None:
    inputs = seed_archive(tmp_path)
    event_path, artifact_path = normalize_telegram_day(inputs, date=DAY)
    # Model the previously published last-import record and its acquired bytes.
    legacy = next(e for e in read_jsonl(event_path) if e["text"] == "Export fixture 2")
    artifact = next(
        a for a in read_jsonl(artifact_path) if a["artifact_id"] == legacy["artifact_ids"][0]
    )
    legacy["event_id"] = "evt_" + hashlib.sha256(b"telegram:personal:5:1048576").hexdigest()[:20]
    artifact["artifact_id"] = (
        "artifact_"
        + hashlib.sha256(
            f"telegram:personal:artifact:1048576:{artifact['source_object_id']}".encode()
        ).hexdigest()[:20]
    )
    legacy["artifact_ids"] = [artifact["artifact_id"]]
    artifact["event_ids"] = [legacy["event_id"]]
    blob = inputs.root / "old-import.bin"
    blob.write_bytes(b"Old acquired fixture bytes")
    artifact.update(
        download_status="downloaded",
        local_path=str(blob),
        checksums={"sha256": hashlib.sha256(blob.read_bytes()).hexdigest()},
    )
    write_jsonl(event_path, [legacy])
    write_jsonl(artifact_path, [artifact])
    old_packet = prepare_journal(inputs, day=DAY, author="Fixture")
    before = snapshot(inputs.root)
    output = RecallPaths.from_root(tmp_path / "replay")
    run = partial(
        rebuild_range,
        inputs,
        output,
        first=DAY,
        last=DAY,
        sources=["telegram"],
        timezone_name="UTC",
    )
    jobs = run()
    assert jobs[0]["status"] == "success" and jobs[0]["event_count"] == 3
    events = read_jsonl(output.normalized_event_path(DAY))
    artifacts = read_jsonl(output.artifact_metadata_path("telegram", DAY))
    assert len(events) == len(artifacts) == 3
    assert all(
        a["download_status"] == "not_requested" and a["checksums"] == {} and a["local_path"] is None
        for a in artifacts
    )
    index = build_index([output])
    results = search(Path(index["index"]), "fixture")
    assert index["events"] == len(results) == 3
    for result in results:
        assert result["event"] == events[result["line"] - 1]
        ref = result["event"]["raw_ref"]
        assert ref["path"] == str(inputs.raw_capture_dir("telegram", DAY) / "updates.jsonl")
        assert ref["locator"]["line"] in (1, 2, 3)
    packet = prepare_journal(output, day=DAY, author="Fixture")
    assert len(_load_packet(packet)[1]) == 3 and len(_load_packet(old_packet)[1]) == 1
    assert snapshot(inputs.root) == before
    replay_before = snapshot(output.root)
    assert run() == jobs
    build_index([output])
    assert prepare_journal(output, day=DAY, author="Fixture") == packet
    assert snapshot(output.root) == replay_before
