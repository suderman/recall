from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_voice_corpus import destination, result, setup
from test_voice_telegram import DAY, command, save

from recall.connectors.telegram.normalize import _event_id
from recall.connectors.telegram.voice import _digest
from recall.voice_corpus import collect, inspect_day


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)


def fixture(tmp_path, *, date_window=False):
    paths, raw, row, event, policy, _ = setup(tmp_path)
    message = row["payload"]["message"]
    message.update({"@type": "message", "chat_id": 321})
    event.update(event_id=_event_id("personal", 321, message["id"]), conversation_id="321")
    event["raw_ref"]["locator"]["chat_id"] = 321
    stamp = message["date"]
    chat = {
        "@type": "chat",
        "id": 321,
        "title": "Fixture",
        "type": {"@type": "chatTypePrivate", "user_id": 321},
    }
    row["payload"]["chat"] = chat
    row["payload"]["users"] = [
        {"@type": "user", "id": uid, "type": {"@type": "userTypeRegular"}} for uid in (123, 321)
    ]
    row["capture_mode"] = "tdlib-history"
    row["update_type"] = row["payload"]["@type"] = "getChatHistoryMessage"
    row["update_id"] = None
    row["received_at"] = datetime.fromtimestamp(stamp + 200, timezone.utc).isoformat()
    report_path = paths.root / "proof/fixture-history.json"
    row["history_report"] = str(report_path)
    row["history"] = {"page": 0, "message_index": 0, "message_sha256": _digest(message)}
    save(paths, raw, row, event)
    report = {
        "chat_id": 321,
        "selected_own": 1,
        "stop": "date_boundary" if date_window else "own_limit",
        "window": [stamp - 50, stamp + 50] if date_window else None,
        "requested_own": None if date_window else 1,
        "chat": chat,
        "chat_response_sha256": "a" * 64,
        "peer_response_sha256": "b" * 64,
        "pages": [
            {
                "query": {
                    "@type": "getChatHistory",
                    "chat_id": 321,
                    "from_message_id": 0,
                    "offset": 0,
                    "limit": 100,
                    "only_local": False,
                },
                "response_sha256": "c" * 64,
                "message_ids": [456, 455] if date_window else [456],
                "dates": [stamp, stamp - 100] if date_window else [stamp],
            }
        ],
    }
    auth = {
        "account": "personal",
        "self": {"@type": "user", "id": 123, "type": {"@type": "userTypeRegular"}},
        "self_response_sha256": "d" * 64,
        "cutoff": stamp + 100,
        "chats": {},
    }
    summary = {
        "account": "personal",
        "self_id": 123,
        "cutoff": stamp + 100,
        "counts": {"fixture": {"own": 1, "pages": 1, "stop": report["stop"]}},
        "raw": str(raw),
        "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "original_state_sha256": {},
        "history_complete": False,
    }
    store(report_path, report)
    store(report_path.parent / "authenticated-self.json", auth)
    store(report_path.parent / "capture-summary.json", summary)
    return paths, raw, row, event, policy, report_path


def run(paths, policy):
    return collect(paths, day=DAY, source="telegram", account="personal", policy_path=policy)


@pytest.mark.parametrize("date_window", [False, True])
def test_honest_history_plaintext_proof_admission_and_retry(tmp_path, date_window):
    paths, raw, _, event, policy, report_path = fixture(tmp_path, date_window=date_window)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (
            raw,
            policy,
            report_path,
            report_path.parent / "authenticated-self.json",
            report_path.parent / "capture-summary.json",
        )
    }
    answer = command(paths)
    assert answer.exit_code == 0, answer.output
    item = json.loads(answer.stdout)["records"][0]
    assert item["corpus_eligible"] is False and item["status"] == "needs_review"
    assert item["passage"]["text"] == event["text"]
    assert item["history_proof"]["version"] == 1 and len(item["proof_inputs"]) == 3
    assert run(paths, policy)["counts"]["eligible"] == 1
    assert len(result(paths)["scopes"][0]["proof_inputs"]) == 3
    assert inspect_day(paths, day=DAY)["verified_current"]
    ledger = destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns
    assert not run(paths, policy)["changed"]
    assert (destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns) == ledger
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    assert not paths.database.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "public",
        "symlink",
        "duplicate_json",
        "auth_account",
        "auth_self",
        "auth_bot",
        "cutoff",
        "raw_path",
        "raw_hash",
        "counts",
        "count_bool",
        "complete",
        "complete_int",
        "chat",
        "peer_bot",
        "report_chat",
        "report_count",
        "count_bool_report",
        "query_type",
        "query_chat",
        "query_offset",
        "query_limit",
        "local",
        "local_int",
        "cursor",
        "ids",
        "dates",
        "page_index",
        "message_index",
        "message_hash",
        "response_hash",
        "received",
        "received_type",
        "update_id",
        "payload_type",
        "stop",
        "own_limit",
        "window",
        "summary_stop",
    ],
)
def test_invalid_history_proof_is_not_empty_and_preserves_corpus(tmp_path, damage):
    paths, raw, row, event, policy, report_path = fixture(tmp_path)
    run(paths, policy)
    before = destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns
    auth_path, summary_path = (
        report_path.parent / name for name in ("authenticated-self.json", "capture-summary.json")
    )
    auth, report, summary = (
        json.loads(p.read_bytes()) for p in (auth_path, report_path, summary_path)
    )
    page = report["pages"][0]
    if damage == "missing":
        auth_path.unlink()
    elif damage == "public":
        auth_path.chmod(0o644)
    elif damage == "symlink":
        moved = auth_path.with_suffix(".other")
        auth_path.rename(moved)
        auth_path.symlink_to(moved)
    elif damage == "duplicate_json":
        auth_path.write_text('{"account":"personal","account":"SECRET"}')
    else:
        if damage == "auth_account":
            auth["account"] = "other"
        elif damage == "auth_self":
            auth["self"]["id"] = 999
        elif damage == "auth_bot":
            auth["self"]["type"]["@type"] = "userTypeBot"
        elif damage == "cutoff":
            auth["cutoff"] = event and row["payload"]["message"]["date"] - 1
        elif damage == "raw_path":
            summary["raw"] = "/elsewhere"
        elif damage == "raw_hash":
            summary["raw_sha256"] = "0" * 64
        elif damage == "counts":
            summary["counts"] = []
        elif damage == "count_bool":
            summary["counts"]["fixture"]["own"] = True
        elif damage == "complete":
            summary["history_complete"] = True
        elif damage == "complete_int":
            summary["history_complete"] = 0
        elif damage == "chat":
            row["payload"]["chat"]["type"] = {"@type": "chatTypeSupergroup", "supergroup_id": 321}
        elif damage == "peer_bot":
            row["payload"]["users"][1]["type"]["@type"] = "userTypeBot"
        elif damage == "report_chat":
            report["chat_id"] = 999
        elif damage == "report_count":
            report["selected_own"] = 2
        elif damage == "count_bool_report":
            report["selected_own"] = True
        elif damage == "query_type":
            page["query"]["@type"] = "getMessages"
        elif damage == "query_chat":
            page["query"]["chat_id"] = 999
        elif damage == "query_offset":
            page["query"]["offset"] = True
        elif damage == "query_limit":
            page["query"]["limit"] = 101
        elif damage == "local":
            page["query"]["only_local"] = True
        elif damage == "local_int":
            page["query"]["only_local"] = 0
        elif damage == "cursor":
            page["query"]["from_message_id"] = 456
        elif damage == "ids":
            page["message_ids"] = [455]
        elif damage == "dates":
            page["dates"] = [1]
        elif damage == "page_index":
            row["history"]["page"] = True
        elif damage == "message_index":
            row["history"]["message_index"] = 10
        elif damage == "message_hash":
            row["history"]["message_sha256"] = "0" * 64
        elif damage == "response_hash":
            page["response_sha256"] = "no"
        elif damage == "received":
            row["received_at"] = "not a timestamp"
        elif damage == "received_type":
            row["received_at"] = 123
        elif damage == "update_id":
            row["update_id"] = 1
        elif damage == "payload_type":
            row["payload"]["@type"] = "updateNewMessage"
        elif damage == "stop":
            report["stop"] = "page_cap"
        elif damage == "own_limit":
            report["requested_own"] = 100
        elif damage == "window":
            report["window"] = [1, 2]
        elif damage == "summary_stop":
            summary["counts"]["fixture"]["stop"] = "missing"
        save(paths, raw, row, event)
        if damage != "raw_hash":
            summary["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        store(auth_path, auth)
        store(report_path, report)
        store(summary_path, summary)
    answer = command(paths)
    assert answer.exit_code == 0, answer.output
    item = json.loads(answer.stdout)["records"][0]
    assert item["passage"] is None and item["reason"].startswith("raw_")
    assert "SECRET" not in answer.stdout
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy)
    assert (destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns) == before
    assert not inspect_day(paths, day=DAY)["verified_current"]


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("photo", "unsupported_content"),
        ("quote", "ambiguous_quotation"),
        ("code", "non_prose"),
        ("forward", "forwarded"),
        ("format", "unsupported_formatting"),
    ],
)
def test_verified_history_preserves_existing_text_exclusions(tmp_path, kind, reason):
    paths, raw, row, event, policy, report_path = fixture(tmp_path)
    message = row["payload"]["message"]
    if kind == "photo":
        message["content"] = {"@type": "messagePhoto"}
    elif kind == "quote":
        message["content"]["text"]["text"] = "> Someone else"
    elif kind == "code":
        message["content"]["text"]["text"] = "`code`"
    elif kind == "forward":
        message["forward_info"] = {"date": 1}
    else:
        message["content"]["text"]["entities"] = [{"type": {"@type": "textEntityTypeBold"}}]
    if kind in {"quote", "code"}:
        event["text"] = message["content"]["text"]["text"]
    row["history"]["message_sha256"] = _digest(message)
    save(paths, raw, row, event)
    summary_path = report_path.parent / "capture-summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    store(summary_path, summary)
    assert run(paths, policy)["counts"]["excluded"] == 1
    item = result(paths)["scopes"][0]["records"][0]
    assert item["reason"] == reason and item["passage"] is None and item["history_proof"]
    assert inspect_day(paths, day=DAY)["verified_current"]


def test_stalled_history_cursor_is_not_a_valid_receipt(tmp_path):
    paths, _, _, _, _, report_path = fixture(tmp_path)
    report = json.loads(report_path.read_bytes())
    page = json.loads(json.dumps(report["pages"][0]))
    page["query"]["from_message_id"] = page["message_ids"][-1]
    report["pages"].append(page)
    store(report_path, report)
    summary_path = report_path.parent / "capture-summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["counts"]["fixture"]["pages"] = 2
    store(summary_path, summary)
    answer = command(paths)
    assert answer.exit_code == 0, answer.output
    item = json.loads(answer.stdout)["records"][0]
    assert item["passage"] is None and item["reason"] == "raw_history_unverifiable"


def test_exact_history_span_grant_does_not_transfer_to_a_coherent_body_edit(tmp_path):
    paths, raw, row, event, policy, report_path = fixture(tmp_path)
    run(paths, policy)
    item = result(paths)["scopes"][0]["records"][0]
    value = json.loads(policy.read_bytes())
    value["origin_grants"][0]["span"] = {
        "event_id": event["event_id"],
        "body_sha256": item["body_sha256"],
        "extractor_version": 1,
        **{key: item["passage"][key] for key in ("start", "end", "sha256")},
    }
    store(policy, value)
    assert run(paths, policy)["counts"]["eligible"] == 1
    message = row["payload"]["message"]
    message["content"]["text"]["text"] = event["text"] = "I wrote a revised reply."
    row["history"]["message_sha256"] = _digest(message)
    save(paths, raw, row, event)
    summary_path = report_path.parent / "capture-summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    store(summary_path, summary)
    assert not inspect_day(paths, day=DAY)["verified_current"]
    report = run(paths, policy)
    assert report["counts"] == {"eligible": 0, "excluded": 0, "needs_review": 1}
    assert result(paths)["scopes"][0]["records"][0]["passage"] is None
    assert inspect_day(paths, day=DAY)["verified_current"]


def test_proof_drift_after_candidates_cannot_publish(tmp_path, monkeypatch):
    import recall.voice_corpus as corpus

    paths, _, _, _, policy, report_path = fixture(tmp_path)
    run(paths, policy)
    before = destination(paths).read_bytes()
    original = corpus.candidates

    def drift(*args, **kwargs):
        value = original(*args, **kwargs)
        report_path.write_bytes(report_path.read_bytes() + b" ")
        return value

    monkeypatch.setattr(corpus, "candidates", drift)
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy)
    assert destination(paths).read_bytes() == before


def test_private_proof_permissions_rechecked_before_write(tmp_path, monkeypatch):
    import recall.voice_corpus as corpus

    paths, _, _, _, policy, report_path = fixture(tmp_path)
    run(paths, policy)
    before = destination(paths).read_bytes()
    original = corpus._unchanged
    calls = []

    def drift(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            report_path.chmod(0o644)
        return original(*args, **kwargs)

    monkeypatch.setattr(corpus, "_unchanged", drift)
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy)
    assert destination(paths).read_bytes() == before


def test_history_reference_relocation_preserves_recorded_proof(tmp_path, monkeypatch):
    paths, _, _, _, policy, report_path = fixture(tmp_path / "original")
    target = tmp_path / "copy"
    shutil.copytree(paths.root, target)
    mapping = tmp_path / "map.json"
    store(mapping, [{"old": str(paths.root), "new": str(target)}])
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    from recall.storage.paths import RecallPaths

    moved = RecallPaths.from_root(target)
    assert run(moved, target / policy.relative_to(paths.root))["verified_current"]
    proof = result(moved)["scopes"][0]["records"][0]
    assert proof["history_proof"]["report_path"] == str(report_path)
    assert all(Path(item["path"]).is_relative_to(target) for item in proof["proof_inputs"])
    assert inspect_day(moved, day=DAY)["verified_current"]


def test_history_inspection_and_collection_do_not_query_or_initialize_state(tmp_path, monkeypatch):
    paths, _, _, _, policy, _ = fixture(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected side effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(type(paths), "ensure_directories", forbidden)
    assert run(paths, policy)["verified_current"]
    assert inspect_day(paths, day=DAY)["verified_current"]
