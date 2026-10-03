from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path

import pytest
import sqlalchemy as sa

from recall.connectors.telegram.capture import append_telegram_update
from recall.connectors.telegram.entities import sync_telegram_entities
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.entities.resolve import match_entities
from recall.entities.storage import upsert_resolutions
from recall.normalize.rebuild import rebuild_range
from recall.search import build_index, search
from recall.storage.db import aliases, connect, identities, identity_aliases, persons, resolutions
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize.journal import _load_packet, inspect_packet, prepare_journal

DAY = "2026-03-31"


def key(account, bundle, kind, *parts):
    encoded = json.dumps(
        ["telegram-export-entity-v1", account, bundle, kind, *map(str, parts)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def native(paths):
    append_telegram_update(
        paths,
        account="personal",
        capture_mode="stream",
        update_type="updateNewMessage",
        received_at=DAY + "T21:00:00Z",
        payload={
            "chat": {"id": 5, "title": "Native", "participant_user_ids": [7]},
            "users": [
                {
                    "id": 7,
                    "first_name": "Native",
                    "last_name": "Person",
                    "usernames": ["same"],
                    "phone_number": "+15555550100",
                }
            ],
            "message": {
                "id": 1048576,
                "chat_id": 5,
                "date": 1774990800,
                "sender_id": {"@type": "messageSenderUser", "user_id": 7},
                "reply_to_message_id": 9,
                "content": {"@type": "messageText", "text": {"text": "Native fixture"}},
            },
        },
    )
    sync_telegram_entities(paths, date=DAY)
    normalize_telegram_day(paths, date=DAY)


def bundle(root: Path, name: str, *, sender=True):
    export = root / name
    export.mkdir()
    (export / "result.json").write_text(
        json.dumps(
            {
                "chats": {
                    "list": [
                        {
                            "id": 5,
                            "name": "Native Person",
                            "messages": [
                                {
                                    "id": 1048576,
                                    "type": "message",
                                    "date": DAY + "T21:00:00Z",
                                    "date_unixtime": "1774990800",
                                    "text": name + " fixture",
                                    "reply_to_message_id": 9,
                                    **(
                                        {"from_id": "user7", "from": "Export Person"}
                                        if sender
                                        else {}
                                    ),
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    return export


def tables(paths):
    with connect(paths) as connection:
        return {
            table.name: [
                dict(row._mapping)
                for row in connection.execute(sa.select(table).order_by(list(table.primary_key)[0]))
            ]
            for table in (persons, identities, aliases, identity_aliases, resolutions)
        }


def test_actual_import_separates_entities_preserves_native_and_manual_state(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "inputs")
    native(paths)
    native_event = read_jsonl(paths.normalized_event_path(DAY))[0]
    native_tables = tables(paths)
    imports = [
        import_telegram_export(paths, export_path=bundle(tmp_path, name), account="personal")
        for name in ("one", "two")
    ]
    sync_telegram_entities(paths, date=DAY)
    event_path, _ = normalize_telegram_day(paths, date=DAY)
    events = read_jsonl(event_path)
    assert next(event for event in events if event["text"] == "Native fixture") == native_event
    current = tables(paths)
    for table, rows in native_tables.items():
        assert all(row in current[table] for row in rows)
    assert len(events) == 3
    assert len({event["conversation_id"] for event in events}) == 3
    assert len({event["thread_id"] for event in events}) == 3
    assert len({event["sender_identity_id"] for event in events}) == 3
    for imported, name in zip(imports, ("one", "two"), strict=True):
        event = next(event for event in events if event["text"] == name + " fixture")
        sender = "ident_telegram_export_user_" + key("personal", imported.import_id, "user", 7)
        assert event["sender_identity_id"] == sender
        assert event["participant_identity_ids"] == [sender]
        assert event["sender_person_id"] is None and event["participant_person_ids"] == []
        assert event["conversation_id"] == "telegram-export:" + key(
            "personal", imported.import_id, "conversation", 5
        )
        assert event["thread_id"] == "telegram-export:" + key(
            "personal", imported.import_id, "reply", 5, 9
        )
        person = next(
            row
            for row in current["persons"]
            if row["person_id"]
            == ("person_telegram_export_" + key("personal", imported.import_id, "person", 7))
        )
        assert person["display_name"] == "Export Person"
        identity = next(row for row in current["identities"] if row["identity_id"] == sender)
        assert identity["kind"] == "export_user" and identity["person_id"] is None
        assert json.loads(identity["value"])[1:3] == ["personal", imported.import_id]
        assert not any(row["identity_id"] == sender for row in current["resolutions"])
    assert match_entities(paths).automatic_resolutions_applied == 0
    linked = events[1]["sender_identity_id"]
    upsert_resolutions(
        paths,
        [
            {
                "resolution_id": "manual_fixture",
                "identity_id": linked,
                "person_id": "person_telegram_user_7",
                "method": "manual",
                "confidence": "high",
                "valid_from": DAY,
                "valid_to": DAY,
                "evidence": ["Explicit fixture link"],
                "created_at": DAY + "T22:00:00Z",
            }
        ],
    )
    before = tables(paths)
    sync_telegram_entities(paths, date=DAY)
    assert tables(paths) == before
    normalize_telegram_day(paths, date=DAY)
    linked_event = next(
        event for event in read_jsonl(event_path) if event["sender_identity_id"] == linked
    )
    assert linked_event["sender_person_id"] == "person_telegram_user_7"


def test_missing_sender_never_infers_chat_title_membership(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "inputs")
    result = import_telegram_export(
        paths, export_path=bundle(tmp_path, "missing", sender=False), account="personal"
    )
    sync_telegram_entities(paths, date=DAY)
    event_path, _ = normalize_telegram_day(paths, date=DAY)
    event = read_jsonl(event_path)[0]
    assert event["sender_identity_id"] is None and event["participant_identity_ids"] == []
    assert tables(paths)["persons"] == tables(paths)["resolutions"] == []
    assert event["conversation_id"] == "telegram-export:" + key(
        "personal", result.import_id, "conversation", 5
    )


@pytest.mark.parametrize("invalid", [None, "", " ", False, 1])
def test_entity_sync_rejects_invalid_import_scope_before_writes(tmp_path, invalid):
    paths = RecallPaths.from_root(tmp_path)
    native(paths)
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    row = read_jsonl(raw)[0]
    row.update(capture_mode="import", import_id=invalid)
    before = tables(paths)
    write_jsonl(raw, [*read_jsonl(raw), row])
    with pytest.raises(ValueError, match="import_id"):
        sync_telegram_entities(paths, date=DAY)
    assert tables(paths) == before


def snapshot(root):
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".lock"
    }


def test_mixed_replay_late_edits_scope_and_citations_repeat(tmp_path):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    native(inputs)
    old_native = read_jsonl(inputs.normalized_event_path(DAY))[0]
    old_packet = prepare_journal(inputs, day=DAY, author="Fixture")
    for name in ("one", "two"):
        path = bundle(tmp_path, name)
        (path / "media.bin").write_bytes(b"Fixture evidence")
        data = json.loads((path / "result.json").read_text())
        data["chats"]["list"][0]["messages"][0].update(file="media.bin")
        (path / "result.json").write_text(json.dumps(data))
        import_telegram_export(inputs, export_path=path, account="personal")
    raw = inputs.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    observation = read_jsonl(raw)[1]
    observation["received_at"] = "2026-04-02T01:00:00Z"
    observation["payload"]["message"]["date"] = 1775005200  # April 1 UTC, March 31 Edmonton.
    observation["payload"]["message"]["content"]["caption"]["text"] = "Latest export fixture"
    write_jsonl(inputs.raw_capture_dir("telegram", "2026-04-02") / "updates.jsonl", [observation])
    before = snapshot(inputs.root)
    output = RecallPaths.from_root(tmp_path / "replay")
    run = partial(
        rebuild_range,
        inputs,
        output,
        first=DAY,
        last="2026-04-01",
        sources=["telegram"],
        timezone_name="America/Edmonton",
    )
    jobs = run()
    assert jobs[0]["status"] == "success" and jobs[0]["event_count"] == 3
    assert jobs[1]["event_count"] == 0
    events = read_jsonl(output.normalized_event_path(DAY))
    assert len(events) == 3
    native_event = next(e for e in events if e["text"] == "Native fixture")
    for field in ("event_id", "sender_identity_id", "conversation_id", "thread_id"):
        assert native_event[field] == old_native[field]
    latest = next(e for e in events if e["text"] == "Latest export fixture")
    assert latest["raw_ref"]["path"].endswith("2026-04-02/updates.jsonl")
    assert latest["raw_ref"]["locator"]["line"] == 1
    assert len({e["sender_identity_id"] for e in events}) == 3
    artifacts = read_jsonl(output.artifact_metadata_path("telegram", DAY))
    assert len(artifacts) == 2
    assert {e["artifact_ids"][0] for e in events if e["artifact_ids"]} == {
        a["artifact_id"] for a in artifacts
    }
    assert all(a["checksums"] == {} for a in artifacts)
    index = Path(build_index([output])["index"])
    results = search(index, "fixture")
    assert len(results) == 3
    for result in results:
        assert result["event"] == events[result["line"] - 1]
        assert result["citation_error"] is None and result["raw_citation_error"] is None
    packet = prepare_journal(output, day=DAY, author="Fixture", timezone_name="America/Edmonton")
    assert len(_load_packet(packet)[1]) == 3
    assert inspect_packet(packet)["unresolved_citations"] == []
    assert len(_load_packet(old_packet)[1]) == 1
    assert snapshot(inputs.root) == before
    after = snapshot(output.root)
    assert run() == jobs
    build_index([output])
    assert (
        prepare_journal(output, day=DAY, author="Fixture", timezone_name="America/Edmonton")
        == packet
    )
    assert snapshot(output.root) == after and snapshot(inputs.root) == before


def test_export_handles_phones_accounts_and_thread_kinds_stay_local(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    native(paths)
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    original = read_jsonl(raw)[0]
    rows = [original]
    for account, import_id, chat, reference in [
        ("a:b", "c", 5, {"media_album_id": 44}),
        ("a", "b:c", 5, {"media_album_id": 44}),
        ("a:b", "c", 6, {"message_thread_id": 44}),
        ("a:b", "résumé", 5, {"reply_to_message_id": 999}),
    ]:
        row = json.loads(json.dumps(original))
        row.update(account=account, import_id=import_id, capture_mode="import")
        row["payload"]["chat"]["id"] = chat
        message = row["payload"]["message"]
        message.update(chat_id=chat)
        message.pop("reply_to_message_id")
        message.update(reference)
        rows.append(row)
    write_jsonl(raw, rows)
    sync_telegram_entities(paths, date=DAY)
    event_path, _ = normalize_telegram_day(paths, date=DAY)
    events = read_jsonl(event_path)
    assert len(events) == 5
    assert len({e["conversation_id"] for e in events}) == 5
    assert len({e["thread_id"] for e in events}) == 5
    for row in rows[1:]:
        message = row["payload"]["message"]
        kind = (
            "reply"
            if "reply_to_message_id" in message
            else ("album" if "media_album_id" in message else "topic")
        )
        target = 999 if kind == "reply" else 44
        assert "telegram-export:" + key(
            row["account"], row["import_id"], kind, message["chat_id"], target
        ) in {e["thread_id"] for e in events}
    current = tables(paths)
    exported = [i for i in current["identities"] if i["kind"].startswith("export_")]
    assert len({(i["kind"], i["value"]) for i in exported}) == len(exported)
    assert all(i["person_id"] is None for i in exported)
    assert match_entities(paths).automatic_resolutions_applied == 0
    # An explicit link in one bundle must not make its observed handles link the others.
    selected = next(i for i in exported if i["kind"] == "export_username")
    with connect(paths) as connection:
        connection.execute(
            sa.update(identities)
            .where(identities.c.identity_id == selected["identity_id"])
            .values(person_id="person_telegram_user_7", valid_from=DAY, valid_to=DAY)
        )
        connection.commit()
    assert match_entities(paths).automatic_resolutions_applied == 0
    before = tables(paths)
    sync_telegram_entities(paths, date=DAY)
    assert tables(paths) == before


def test_actual_channel_sender_is_not_an_export_person(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "inputs")
    directory = bundle(tmp_path, "channel")
    data = json.loads((directory / "result.json").read_text())
    data["chats"]["list"][0]["messages"][0].update(
        from_id="channel7", **{"from": "Fixture channel"}
    )
    (directory / "result.json").write_text(json.dumps(data))
    result = import_telegram_export(paths, export_path=directory, account="personal")
    sync_telegram_entities(paths, date=DAY)
    event_path, _ = normalize_telegram_day(paths, date=DAY)
    event = read_jsonl(event_path)[0]
    assert event["sender_identity_id"] == "ident_telegram_export_chat_" + key(
        "personal", result.import_id, "chat", 7
    )
    current = tables(paths)
    assert current["persons"] == current["resolutions"] == []
    assert any(row["value"] == "Fixture channel" for row in current["identity_aliases"])
