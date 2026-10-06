from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import subprocess

import pytest
from test_voice_telegram import DAY, SELF, fixture, save
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.voice_corpus import collect, inspect_day


def setup(tmp_path, *, granted=True):
    paths, raw, row, event = fixture(tmp_path)
    ownership = {
        "id": "self",
        "source": "telegram",
        "account": "personal",
        "identity": SELF,
        "from": DAY + "T00:00:00Z",
        "to": DAY + "T23:59:59Z",
        "conversations": None,
    }
    grant = {
        "id": "original",
        "ownership_id": "self",
        "from": ownership["from"],
        "to": ownership["to"],
        "conversations": None,
        "assertion": "original-human-unassisted",
    }
    policy = {
        "format": "recall-voice-policy-v1",
        "ownerships": [ownership],
        "origin_grants": [grant] if granted else [],
        "denials": [],
        "exclude_events": [],
    }
    policy_path = paths.config / "voice-policy.json"
    write_policy(policy_path, policy)
    return paths, raw, row, event, policy_path, policy


def write_policy(path, policy):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, sort_keys=True))
    path.chmod(0o600)


def run(paths, policy, *, source="telegram", account="personal"):
    return collect(paths, day=DAY, source=source, account=account, policy_path=policy)


def destination(paths):
    return paths.derived / "voice" / DAY[:4] / (DAY + ".json")


def result(paths):
    return json.loads(destination(paths).read_bytes())


def record(paths):
    return result(paths)["scopes"][0]["records"][0]


def test_admit_only_with_both_grants_and_retry_is_byte_and_mtime_noop(tmp_path):
    paths, raw, _, event, policy_path, _ = setup(tmp_path)
    protected = {p: p.read_bytes() for p in (raw, paths.normalized_event_path(DAY), policy_path)}
    report = run(paths, policy_path)
    assert report["changed"] and report["verified_current"]
    item = record(paths)
    assert item["status"] == "eligible" and item["corpus_eligible"] is True
    assert item["passage"]["text"] == event["text"]
    assert item["normalized_record_sha256"]
    assert item["origin_grant_sha256"] and item["ownership_sha256"] and item["span_id"]
    before = destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns
    assert run(paths, policy_path)["changed"] is False
    assert (destination(paths).read_bytes(), destination(paths).stat().st_mtime_ns) == before
    assert {p: p.read_bytes() for p in protected} == protected
    assert inspect_day(paths, day=DAY)["verified_current"] is True
    assert not paths.database.exists()
    for p in (paths.derived / "voice", destination(paths).parent):
        assert p.stat().st_mode & 0o777 == 0o700
    assert destination(paths).stat().st_mode & 0o777 == 0o600


def test_missing_origin_grant_keeps_prose_out_of_saved_report(tmp_path):
    paths, _, _, event, policy_path, _ = setup(tmp_path, granted=False)
    run(paths, policy_path)
    item = record(paths)
    assert item["status"] == "needs_review" and item["passage"] is None
    assert item["corpus_eligible"] is False and event["text"] not in destination(paths).read_text()


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("ownership", "identity_not_owned"),
        ("time", "identity_not_owned"),
        ("conversation", "identity_not_owned"),
        ("overlap_owner", "ambiguous_ownership"),
        ("overlap_grant", "ambiguous_origin_grant"),
        ("exclude", "explicitly_excluded"),
        ("deny", "origin_denied"),
        ("generated", "known_generated"),
    ],
)
def test_grants_cannot_override_exclusions_ambiguity_or_scope(tmp_path, damage, reason):
    paths, raw, row, event, policy_path, policy = setup(tmp_path)
    if damage == "ownership":
        policy["ownerships"] = []
        policy["origin_grants"] = []
    elif damage == "time":
        policy["ownerships"][0]["to"] = DAY + "T17:00:00Z"
        policy["origin_grants"] = []
    elif damage == "conversation":
        policy["ownerships"][0]["conversations"] = ["elsewhere"]
        policy["origin_grants"] = []
    elif damage == "overlap_owner":
        owner = {**policy["ownerships"][0], "id": "duplicate"}
        policy["ownerships"].append(owner)
    elif damage == "overlap_grant":
        policy["origin_grants"].append({**policy["origin_grants"][0], "id": "duplicate"})
    elif damage == "exclude":
        policy["exclude_events"] = [event["event_id"]]
    elif damage == "deny":
        policy["denials"] = [{**policy["ownerships"][0], "id": "deny"}]
    else:
        event["tags"] = ["ai_assisted"]
        save(paths, raw, row, event)
    write_policy(policy_path, policy)
    run(paths, policy_path)
    assert record(paths)["reason"] == reason and record(paths)["passage"] is None
    assert record(paths)["corpus_eligible"] is False


def test_edit_replaces_span_and_revocation_removes_admission(tmp_path):
    paths, raw, row, event, policy_path, policy = setup(tmp_path)
    run(paths, policy_path)
    old_id = record(paths)["span_id"]
    event["text"] = "I changed my mind."
    row["payload"]["message"]["content"]["text"]["text"] = event["text"]
    save(paths, raw, row, event)
    assert not inspect_day(paths, day=DAY)["verified_current"]
    run(paths, policy_path)
    assert record(paths)["span_id"] != old_id and len(result(paths)["scopes"][0]["records"]) == 1
    policy["origin_grants"] = []
    write_policy(policy_path, policy)
    assert not inspect_day(paths, day=DAY)["verified_current"]
    run(paths, policy_path)
    assert record(paths)["status"] == "needs_review" and record(paths)["passage"] is None


def test_exact_span_receipt_never_transfers_to_edited_text(tmp_path):
    paths, raw, row, event, policy_path, policy = setup(tmp_path)
    digest = hashlib.sha256(event["text"].encode()).hexdigest()
    policy["origin_grants"][0]["span"] = {
        "event_id": event["event_id"],
        "body_sha256": digest,
        "start": 0,
        "end": len(event["text"]),
        "sha256": digest,
        "extractor_version": 1,
    }
    write_policy(policy_path, policy)
    run(paths, policy_path)
    assert record(paths)["corpus_eligible"]
    event["text"] += " Later edit."
    row["payload"]["message"]["content"]["text"]["text"] = event["text"]
    save(paths, raw, row, event)
    run(paths, policy_path)
    assert record(paths)["status"] == "needs_review" and record(paths)["passage"] is None


@pytest.mark.parametrize(
    "damage",
    [
        "missing_normalized",
        "malformed",
        "truncated",
        "empty",
        "missing_raw",
        "raw_mismatch",
        "duplicate",
        "bad_policy",
    ],
)
def test_failed_input_keeps_previous_result_and_does_not_certify_freshness(tmp_path, damage):
    paths, raw, row, event, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    old = destination(paths).read_bytes()
    normalized = paths.normalized_event_path(DAY)
    if damage == "missing_normalized":
        normalized.unlink()
    elif damage == "malformed":
        normalized.write_text('{"PRIVATE SECRET":')
    elif damage == "truncated":
        normalized.write_bytes(normalized.read_bytes().rstrip(b"\n"))
    elif damage == "empty":
        normalized.write_bytes(b"")
    elif damage == "missing_raw":
        raw.unlink()
    elif damage == "raw_mismatch":
        row["payload"]["message"]["content"]["text"]["text"] = "DIFFERENT SECRET"
        save(paths, raw, row, event)
    elif damage == "duplicate":
        write_jsonl(normalized, [event, event])
    else:
        policy_path.write_text('{"PRIVATE SECRET":')
    with pytest.raises(ValueError, match="not verified current") as failure:
        run(paths, policy_path)
    assert "SECRET" not in str(failure.value) and destination(paths).read_bytes() == old
    assert not inspect_day(paths, day=DAY)["verified_current"]


def test_complete_line_truncation_cannot_remove_previous_scope_events(tmp_path):
    paths, _, _, event, policy_path, _ = setup(tmp_path)
    other = {**event, "event_id": "evt_unowned", "sender_identity_id": "someone_else"}
    normalized = paths.normalized_event_path(DAY)
    write_jsonl(normalized, [other, event])
    run(paths, policy_path)
    before = destination(paths).read_bytes()
    normalized.write_bytes(normalized.read_bytes().splitlines(keepends=True)[0])
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy_path)
    assert destination(paths).read_bytes() == before
    assert not inspect_day(paths, day=DAY)["verified_current"]


def test_successful_scope_retains_other_source_account_records(tmp_path):
    from test_voice import SELF as EMAIL_SELF
    from test_voice import fixture as email_fixture

    paths, _, _, telegram, policy_path, policy = setup(tmp_path)
    run(paths, policy_path)
    old_scope = copy.deepcopy(result(paths)["scopes"][0])
    _, _, _, email = email_fixture(paths.root)
    write_jsonl(paths.normalized_event_path(DAY), [telegram, email])
    owner = {
        **policy["ownerships"][0],
        "id": "email",
        "source": "email",
        "account": "work",
        "identity": EMAIL_SELF,
    }
    policy["ownerships"].append(owner)
    policy["origin_grants"].append(
        {**policy["origin_grants"][0], "id": "mail", "ownership_id": "email"}
    )
    write_policy(policy_path, policy)
    run(paths, policy_path, source="email", account="work")
    scopes = result(paths)["scopes"]
    assert len(scopes) == 2 and next(s for s in scopes if s["source"] == "telegram") == old_scope
    assert next(s for s in scopes if s["source"] == "email")["records"][0]["corpus_eligible"]
    assert not inspect_day(paths, day=DAY)["verified_current"]


@pytest.mark.parametrize(
    "damage",
    [
        "public_policy",
        "symlink_policy",
        "unknown_field",
        "missing_assertion",
        "non_utc",
        "reverse",
        "unknown_owner",
    ],
)
def test_invalid_policy_cannot_create_corpus(tmp_path, damage):
    paths, _, _, _, policy_path, policy = setup(tmp_path)
    if damage == "public_policy":
        policy_path.chmod(0o644)
    elif damage == "symlink_policy":
        original = policy_path.with_suffix(".original")
        policy_path.rename(original)
        policy_path.symlink_to(original)
    else:
        if damage == "unknown_field":
            policy["grant_everything"] = True
        elif damage == "missing_assertion":
            policy["origin_grants"][0].pop("assertion")
        elif damage == "non_utc":
            policy["ownerships"][0]["from"] = "2026-10-01T00:00:00-06:00"
        elif damage == "reverse":
            policy["ownerships"][0]["to"] = "2026-09-30T00:00:00Z"
        else:
            policy["origin_grants"][0]["ownership_id"] = "unknown"
        write_policy(policy_path, policy)
    with pytest.raises(ValueError):
        run(paths, policy_path)
    assert not (paths.derived / "voice").exists()


def test_one_writer_and_atomic_failure_preserve_previous_bytes(tmp_path, monkeypatch):
    paths, _, _, _, policy_path, policy = setup(tmp_path)
    run(paths, policy_path)
    old = destination(paths).read_bytes()
    lock_path = paths.derived / "voice/.writer.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="writer"):
            run(paths, policy_path)
    policy["origin_grants"] = []
    write_policy(policy_path, policy)

    def fail_replace(*args):
        raise OSError("PRIVATE SECRET")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ValueError, match="not verified current") as failure:
        run(paths, policy_path)
    assert "SECRET" not in str(failure.value) and destination(paths).read_bytes() == old
    assert list(destination(paths).parent.iterdir()) == [destination(paths)]


def test_inspect_cannot_certify_input_changed_after_scope_evaluation(tmp_path, monkeypatch):
    import recall.voice_corpus as corpus

    paths, raw, _, _, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    original = corpus._evaluate

    def drift(*args, **kwargs):
        evaluated = original(*args, **kwargs)
        raw.write_bytes(raw.read_bytes() + b"\n")
        return evaluated

    monkeypatch.setattr(corpus, "_evaluate", drift)
    assert not inspect_day(paths, day=DAY)["verified_current"]


@pytest.mark.parametrize("input_name", ["raw", "normalized", "policy"])
def test_changing_input_after_extraction_preserves_previous_result(
    tmp_path, monkeypatch, input_name
):
    import recall.voice_corpus as corpus

    paths, raw, _, _, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    before = destination(paths).read_bytes()
    changed = {"raw": raw, "normalized": paths.normalized_event_path(DAY), "policy": policy_path}[
        input_name
    ]
    original = corpus.candidates

    def drift(*args, **kwargs):
        report = original(*args, **kwargs)
        changed.write_bytes(changed.read_bytes() + b"\n")
        return report

    monkeypatch.setattr(corpus, "candidates", drift)
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy_path)
    assert destination(paths).read_bytes() == before


@pytest.mark.parametrize("target", ["voice", "year", "file", "lock"])
@pytest.mark.parametrize("damage", ["public", "symlink"])
def test_public_or_symlink_outputs_are_not_replaced(tmp_path, target, damage):
    paths, _, _, _, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    before = destination(paths).read_bytes()
    selected = {
        "voice": paths.derived / "voice",
        "year": destination(paths).parent,
        "file": destination(paths),
        "lock": paths.derived / "voice/.writer.lock",
    }[target]
    if damage == "public":
        selected.chmod(0o755 if selected.is_dir() else 0o644)
    else:
        moved = selected.with_name(selected.name + ".outside")
        selected.rename(moved)
        selected.symlink_to(moved)
    with pytest.raises(ValueError, match="not verified current"):
        run(paths, policy_path)
    assert destination(paths).read_bytes() == before


def test_full_scope_not_limited_to_candidate_cli_display_cap(tmp_path):
    paths, _, _, event, policy_path, _ = setup(tmp_path)
    others = [
        {**event, "event_id": f"evt_other_{number}", "sender_identity_id": "someone_else"}
        for number in range(201)
    ]
    write_jsonl(paths.normalized_event_path(DAY), [*others, event])
    report = run(paths, policy_path)
    assert report["counts"] == {"eligible": 1, "excluded": 201, "needs_review": 0}
    assert len(result(paths)["scopes"][0]["records"]) == 202


def test_raw_append_changes_snapshot_not_span_identity(tmp_path):
    paths, raw, _, _, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    span_id = record(paths)["span_id"]
    raw.write_bytes(raw.read_bytes() + b'{"unrelated":true}\n')
    assert not inspect_day(paths, day=DAY)["verified_current"]
    run(paths, policy_path)
    assert record(paths)["span_id"] == span_id
    assert inspect_day(paths, day=DAY)["verified_current"]


def test_inspect_detects_tampered_admission_without_writing(tmp_path):
    paths, _, _, _, policy_path, _ = setup(tmp_path)
    run(paths, policy_path)
    stored = result(paths)
    stored["scopes"][0]["records"][0]["passage"]["text"] = "Changed by hand."
    destination(paths).write_text(json.dumps(stored))
    before = destination(paths).read_bytes()
    assert not inspect_day(paths, day=DAY)["verified_current"]
    assert destination(paths).read_bytes() == before


def test_collect_and_inspect_cli_do_not_use_sqlite_network_or_models(tmp_path, monkeypatch):
    paths, _, _, _, policy_path, _ = setup(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Collector crossed saved-input-only boundary")

    monkeypatch.setattr(RecallPaths, "ensure_directories", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    runner = CliRunner()
    args = [
        "voice",
        "collect",
        "--root",
        str(paths.root),
        "--policy",
        str(policy_path),
        "--source",
        "telegram",
        "--account",
        "personal",
        "--date",
        DAY,
    ]
    output = runner.invoke(app, args)
    assert output.exit_code == 0, output.output
    assert json.loads(output.stdout)["verified_current"]
    assert "Café" not in output.stdout
    args = ["voice", "inspect", "--root", str(paths.root), "--date", DAY, "--require-current"]
    assert runner.invoke(app, args).exit_code == 0
    policy_path.unlink()
    output = runner.invoke(app, args)
    assert output.exit_code != 0 and json.loads(output.stdout)["verified_current"] is False
