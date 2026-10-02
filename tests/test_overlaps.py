from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.connectors.bluebubbles.capture import append_bluebubbles_event
from recall.connectors.bluebubbles.importer import import_bluebubbles_export
from recall.connectors.telegram.capture import append_telegram_update
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.dedupe import inspect_overlaps
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

runner = CliRunner()


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_overlaps_reports_bluebubbles_live_import_observations_without_writes(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    message = {"guid": "msg-1", "chatGuid": "chat-1", "text": "original"}
    raw = paths.raw_capture_dir("bluebubbles", "2026-03-31") / "events.jsonl"
    write_jsonl(
        raw,
        [
            {
                "source": "bluebubbles",
                "account": "personal",
                "capture_mode": "webhook",
                "event_type": "new-message",
                "payload": {"data": message},
            },
            {
                "source": "bluebubbles",
                "account": "personal",
                "capture_mode": "import",
                "import_id": "export-1",
                "event_type": "historical-message",
                "payload": {"data": {**message, "text": "edited"}},
            },
        ],
    )
    before = snapshot(tmp_path)

    result = runner.invoke(app, ["events", "overlaps", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["read_only"] is True
    assert report["records_scanned"] == 2
    assert report["message_observations"] == 2
    assert report["normalization_collisions"] == []
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["reason"] == "same_source_message_id"
    assert group["key"] == {
        "source": "bluebubbles",
        "account": "personal",
        "namespace": "native",
        "conversation_id": "chat-1",
        "message_id": "msg-1",
    }
    assert group["payload_variants"] == 2
    assert [r["raw_ref"]["locator"]["line"] for r in group["observations"]] == [1, 2]
    assert [r["capture_mode"] for r in group["observations"]] == ["webhook", "import"]
    assert group["observations"][1]["import_id"] == "export-1"
    for n, observation in enumerate(group["observations"]):
        assert observation["raw_ref"]["path"] == paths.relative_to_root(raw)
        row = json.loads(raw.read_text().splitlines()[n])
        digest = hashlib.sha256(
            json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert observation["record_sha256"] == digest
    assert snapshot(tmp_path) == before
    repeat = runner.invoke(app, ["events", "overlaps", "--root", str(tmp_path)])
    assert repeat.exit_code == 0 and repeat.stdout == result.stdout


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"payload": [1]},
        {"payload": {"message": [1]}},
    ],
)
def test_overlaps_rejects_invalid_envelope_shape_with_physical_line(tmp_path, record) -> None:
    raw = RecallPaths.from_root(tmp_path).raw_capture_dir("telegram", "2026-03-31")
    raw.mkdir(parents=True)
    path = raw / "updates.jsonl"
    path.write_text("\n" + json.dumps(record) + "\n", encoding="utf-8")
    before = snapshot(tmp_path)

    result = runner.invoke(app, ["events", "overlaps", "--root", str(tmp_path)])

    assert result.exit_code == 2, result.output
    with pytest.raises(ValueError) as error:
        inspect_overlaps(RecallPaths.from_root(tmp_path), sources=["telegram"])
    assert str(error.value).endswith(f"{path}:2")
    assert snapshot(tmp_path) == before


def telegram_row(**fields) -> dict:
    return {
        "source": "telegram",
        "account": "personal",
        "capture_mode": "stream",
        "payload": {
            "message": {
                "chat_id": 5,
                "id": 6,
                "content": {"@type": "messageText", "text": {"text": "same text"}},
            }
        },
        **fields,
    }


def test_overlaps_keeps_telegram_exports_accounts_and_conversations_separate(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    native = telegram_row()
    export = telegram_row(capture_mode="import", import_id="bundle-one")
    other_chat = telegram_row()
    other_chat["payload"]["message"]["chat_id"] = 7
    rows = [
        native,
        native,
        telegram_row(account="work"),
        other_chat,
        export,
        export,
        telegram_row(capture_mode="import", import_id="bundle-two"),
        telegram_row(capture_mode="import"),
        {"account": "personal", "payload": {"@type": "updateUser", "id": 6}},
    ]
    write_jsonl(paths.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl", rows)

    report = inspect_overlaps(paths, sources=["telegram"])

    assert report["records_scanned"] == 9
    assert report["message_observations"] == 8
    assert report["non_message_records"] == 1
    assert len(report["groups"]) == 2
    assert {g["key"]["namespace"] for g in report["groups"]} == {"native", "export:bundle-one"}
    assert all(len(g["observations"]) == 2 for g in report["groups"])
    assert all(g["payload_variants"] == 1 for g in report["groups"])
    assert {g["reason"] for g in report["groups"]} == {
        "same_source_message_id",
        "same_import_message_key",
    }
    assert report["unidentified_messages"][0]["reason"] == "missing_import_id"
    collision = report["normalization_collisions"][0]
    assert len(report["normalization_collisions"]) == 1
    assert collision["payload_variants"] == 1  # Equal text does not prove namespace equivalence.
    assert len(collision["namespaces"]) == 3
    assert sum(len(n["observations"]) for n in collision["namespaces"]) == 5
    work = inspect_overlaps(paths, sources=["telegram"], account="work")
    assert work["records_scanned"] == 1 and work["message_observations"] == 1
    assert work["groups"] == [] and work["unidentified_messages"] == []
    assert work["normalization_collisions"] == []


@pytest.mark.parametrize("message_id", [None, 0, "0", True, False, ""])
def test_overlaps_does_not_infer_missing_message_ids_from_text(tmp_path, message_id) -> None:
    paths = RecallPaths.from_root(tmp_path)
    row = telegram_row()
    row["payload"]["message"]["id"] = message_id
    write_jsonl(paths.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl", [row, row])

    report = inspect_overlaps(paths, sources=["telegram"])

    assert report["groups"] == []
    assert report["normalization_collisions"] == []
    assert len(report["unidentified_messages"]) == 2
    assert all(r["reason"] == "missing_source_message_key" for r in report["unidentified_messages"])


def test_overlaps_retains_physical_lines_across_receipt_days(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    for day, blanks in [("2026-03-31", 1), ("2026-04-01", 2)]:
        raw = paths.raw_capture_dir("telegram", day) / "updates.jsonl"
        raw.parent.mkdir(parents=True)
        raw.write_text("\n" * blanks + json.dumps(telegram_row()) + "\n", encoding="utf-8")

    report = inspect_overlaps(paths, sources=["telegram"])

    refs = [r["raw_ref"] for r in report["groups"][0]["observations"]]
    assert [r["locator"]["line"] for r in refs] == [2, 3]
    assert refs[0]["path"] != refs[1]["path"]


def test_overlaps_empty_workspace_does_not_initialize_storage(tmp_path) -> None:
    root = tmp_path / "absent"

    result = runner.invoke(app, ["events", "overlaps", "--root", str(root)])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["records_scanned"] == 0 and report["groups"] == []
    assert not root.exists()


@pytest.mark.parametrize("sources", [["slack"], ["telegram", "telegram"], []])
def test_overlaps_rejects_unsupported_or_duplicate_sources(tmp_path, sources) -> None:
    with pytest.raises(ValueError, match="Choose distinct sources"):
        inspect_overlaps(RecallPaths.from_root(tmp_path), sources=sources)


def test_overlaps_uses_actual_bluebubbles_imported_guid_and_chat(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path / "archive")
    message = {
        "guid": "message-native-1",
        "chats": [{"guid": "chat-native-1"}],
        "dateCreated": 1774990800000,
        "text": "original",
    }
    append_bluebubbles_event(
        paths,
        account="personal",
        payload={"type": "new-message", "data": message},
        received_at="2026-03-31T21:00:00Z",
    )
    bundle = tmp_path / "export"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"export_id":"bundle-one"}', encoding="utf-8")
    write_jsonl(bundle / "messages.jsonl", [{**message, "text": "edited"}])
    imported = import_bluebubbles_export(paths, export_path=bundle, account="personal")
    before = snapshot(tmp_path)

    report = inspect_overlaps(paths, sources=["bluebubbles"])

    assert imported.messages_imported == 1
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["key"]["message_id"] == "message-native-1"
    assert group["key"]["conversation_id"] == "chat-native-1"
    assert group["payload_variants"] == 2
    assert group["observations"][1]["import_id"] == imported.import_id
    assert snapshot(tmp_path) == before


def test_overlaps_marks_actual_telegram_synthesized_import_keys_as_candidates(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path / "archive")
    bundle = tmp_path / "export"
    bundle.mkdir()
    # Missing original chat/message IDs produce importer-generated keys.
    message = {"type": "message", "date": "2026-03-31T21:00:00Z", "text": "same text"}
    (bundle / "result.json").write_text(
        json.dumps(
            {"chats": {"list": [{"name": "No native IDs", "messages": [message, message]}]}}
        ),
        encoding="utf-8",
    )
    imported = import_telegram_export(paths, export_path=bundle, account="personal")
    before = snapshot(tmp_path)

    report = inspect_overlaps(paths, sources=["telegram"])

    assert imported.messages_imported == 2
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["reason"] == "same_import_message_key"
    assert group["key"]["namespace"] == f"export:{imported.import_id}"
    assert snapshot(tmp_path) == before


def test_overlaps_warns_when_telegram_import_namespaces_share_normalized_id(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path / "archive")
    native = telegram_row()["payload"]
    native["message"]["id"] = 1048576
    native["message"]["date"] = 1774990800
    append_telegram_update(
        paths,
        account="personal",
        payload=native,
        update_type="updateNewMessage",
        capture_mode="stream",
        received_at="2026-03-31T21:00:00Z",
    )
    expected_lines = {"native": 1}
    for number in (1, 2):
        bundle = tmp_path / f"export-{number}"
        bundle.mkdir()
        (bundle / "result.json").write_text(
            json.dumps(
                {
                    "chats": {
                        "list": [
                            {
                                "id": 5,
                                "name": "Synthetic collision",
                                "messages": [
                                    {
                                        "id": 1048576,
                                        "type": "message",
                                        "date": "2026-03-31T21:00:00Z",
                                        "date_unixtime": "1774990800",
                                        "text": f"Export {number}",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        imported = import_telegram_export(paths, export_path=bundle, account="personal")
        expected_lines[f"export:{imported.import_id}"] = number + 1
    normalized, _ = normalize_telegram_day(paths, date="2026-03-31")
    events = read_jsonl(normalized)
    # Current normalization omits the audit namespace and selects the final export.
    assert len(events) == 1 and events[0]["text"] == "Export 2"
    assert events[0]["raw_ref"]["locator"]["line"] == 3
    before = snapshot(tmp_path)

    result = runner.invoke(app, ["events", "overlaps", "--root", str(paths.root)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["groups"] == []  # No repeated key within any single audit namespace.
    assert len(report["normalization_collisions"]) == 1
    collision = report["normalization_collisions"][0]
    assert collision["reason"] == "telegram_namespace_not_in_event_id"
    assert collision["event_id"] == events[0]["event_id"]
    assert collision["key"] == {
        "source": "telegram",
        "account": "personal",
        "conversation_id": "5",
        "message_id": "1048576",
    }
    assert collision["payload_variants"] == 3
    for namespace in collision["namespaces"]:
        observation = namespace["observations"][0]
        assert len(namespace["observations"]) == 1
        assert observation["raw_ref"]["locator"]["line"] == expected_lines[namespace["namespace"]]
        raw = paths.root / observation["raw_ref"]["path"]
        row = json.loads(raw.read_text().splitlines()[expected_lines[namespace["namespace"]] - 1])
        assert (
            observation["record_sha256"]
            == hashlib.sha256(
                json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
    repeat = runner.invoke(app, ["events", "overlaps", "--root", str(paths.root)])
    assert repeat.exit_code == 0 and repeat.stdout == result.stdout
    assert snapshot(tmp_path) == before


def test_overlaps_warns_for_two_exports_without_native_capture(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    rows = [telegram_row(capture_mode="import", import_id=bundle) for bundle in ("one", "two")]
    write_jsonl(paths.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl", rows)
    before = snapshot(tmp_path)

    report = inspect_overlaps(paths, sources=["telegram"])

    assert report["groups"] == []
    assert len(report["normalization_collisions"]) == 1
    collision = report["normalization_collisions"][0]
    assert collision["payload_variants"] == 1
    assert [n["namespace"] for n in collision["namespaces"]] == ["export:one", "export:two"]
    lines = [n["observations"][0]["raw_ref"]["locator"]["line"] for n in collision["namespaces"]]
    assert lines == [1, 2]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("change", ["account", "chat", "message", "same_namespace"])
def test_overlaps_does_not_warn_for_separate_keys_or_one_import_namespace(tmp_path, change) -> None:
    paths = RecallPaths.from_root(tmp_path)
    original = telegram_row(capture_mode="import", import_id="one")
    other = telegram_row(capture_mode="import", import_id="two")
    if change == "account":
        other["account"] = "work"
    elif change == "chat":
        other["payload"]["message"]["chat_id"] = 7
    elif change == "message":
        other["payload"]["message"]["id"] = 8
    else:
        other["import_id"] = "one"
    write_jsonl(
        paths.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl", [original, other]
    )

    report = inspect_overlaps(paths, sources=["telegram"])

    assert report["normalization_collisions"] == []
