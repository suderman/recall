from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_voice import fixture as mail_fixture
from test_voice_corpus import destination, result, setup, write_policy
from test_voice_telegram import DAY
from typer.testing import CliRunner

from recall.cli.main import app
from recall.normalize.rebuild import rebuild_range
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.voice_corpus import collect, inspect_day


def replay_fixture(tmp_path):
    inputs, raw, row, _, policy_path, _ = setup(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    # Present capture-day evidence is required for a later empty contribution.
    write_jsonl(inputs.raw_capture_dir("telegram", DAY) / "updates.jsonl", [])
    return inputs, output, raw, row, policy_path


def replay(inputs, output, **kwargs):
    return rebuild_range(
        inputs, output, first=DAY, last=DAY, sources=["telegram"], account="personal", **kwargs
    )


def manifest(output):
    return output.state / "rebuild/manifest.jsonl"


def collect_with_receipt(output, policy):
    return collect(
        output,
        day=DAY,
        source="telegram",
        account="personal",
        policy_path=policy,
        coverage_path=manifest(output),
    )


def test_receipt_allows_partial_and_empty_removal_and_retry(tmp_path):
    inputs, output, raw, row, policy = replay_fixture(tmp_path)
    other = copy.deepcopy(row)
    other["payload"]["message"]["id"] += 1
    other["payload"]["message"]["sender_id"]["user_id"] = 321
    other["payload"]["users"][0]["id"] = 321
    write_jsonl(raw, [row, other])
    replay(inputs, output)
    collect_with_receipt(output, policy)
    assert len(result(output)["scopes"][0]["records"]) == 2
    write_jsonl(raw, [other])
    replay(inputs, output)
    collect_with_receipt(output, policy)
    assert len(result(output)["scopes"][0]["records"]) == 1
    write_jsonl(raw, [])
    replay(inputs, output)
    report = collect_with_receipt(output, policy)
    assert report["counts"] == {"eligible": 0, "needs_review": 0, "excluded": 0}
    assert result(output)["scopes"][0]["records"] == []
    assert inspect_day(output, day=DAY)["verified_current"]
    before = destination(output).read_bytes(), destination(output).stat().st_mtime_ns
    assert not collect_with_receipt(output, policy)["changed"]
    assert (destination(output).read_bytes(), destination(output).stat().st_mtime_ns) == before
    assert not inputs.database.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "normalized_prefix",
        "empty_normalized",
        "missing_normalized",
        "cache",
        "raw",
        "inputs",
        "status",
        "account",
        "source",
        "count",
        "dates",
        "error",
        "duplicate",
        "missing_manifest",
        "public",
        "symlink",
    ],
)
def test_bad_receipt_or_input_preserves_previous_corpus(tmp_path, damage):
    inputs, output, raw, _, policy = replay_fixture(tmp_path)
    replay(inputs, output)
    collect_with_receipt(output, policy)
    before = destination(output).read_bytes()
    receipt = manifest(output)
    jobs = read_jsonl(receipt)
    if damage in {"normalized_prefix", "empty_normalized"}:
        output.normalized_event_path(DAY).write_bytes(b"")
    elif damage == "missing_normalized":
        output.normalized_event_path(DAY).unlink()
    elif damage == "cache":
        (output.state / "rebuild/telegram-events.jsonl").write_bytes(b"")
    elif damage == "raw":
        raw.write_bytes(raw.read_bytes() + b"\n")
    elif damage == "inputs":
        (output.root / jobs[0]["input_manifest"]).write_bytes(b"")
    elif damage == "missing_manifest":
        receipt.unlink()
    elif damage == "public":
        receipt.chmod(0o644)
    elif damage == "symlink":
        moved = receipt.with_suffix(".other")
        receipt.rename(moved)
        receipt.symlink_to(moved)
    else:
        if damage == "status":
            jobs[0]["status"] = "failed"
        elif damage == "account":
            jobs[0]["options"]["account"] = "work"
        elif damage == "source":
            jobs[0]["source"] = "email"
        elif damage == "count":
            jobs[0]["event_count"] += 1
        elif damage == "dates":
            jobs[0]["options"]["dates"] = DAY
        elif damage == "error":
            jobs[0]["error"] = "Fixture error"
        else:
            jobs.append(jobs[0])
        write_jsonl(receipt, jobs)
    with pytest.raises(ValueError, match="not verified current"):
        collect_with_receipt(output, policy)
    assert destination(output).read_bytes() == before
    assert not inspect_day(output, day=DAY)["verified_current"]


def test_unrelated_manifest_job_does_not_invalidate_scope_receipt(tmp_path):
    inputs, output, _, _, policy = replay_fixture(tmp_path)
    replay(inputs, output)
    collect_with_receipt(output, policy)
    jobs = read_jsonl(manifest(output))
    write_jsonl(manifest(output), [*jobs, {"source": "calendar", "date": "2026-09-01"}])
    assert inspect_day(output, day=DAY)["verified_current"]
    assert not collect_with_receipt(output, policy)["changed"]


def test_receipt_change_during_evaluation_preserves_result(tmp_path, monkeypatch):
    import recall.voice_corpus as corpus

    inputs, output, _, _, policy = replay_fixture(tmp_path)
    replay(inputs, output)
    collect_with_receipt(output, policy)
    before = destination(output).read_bytes()
    original = corpus.candidates

    def drift(*args, **kwargs):
        report = original(*args, **kwargs)
        manifest(output).write_bytes(manifest(output).read_bytes() + b"\n")
        return report

    monkeypatch.setattr(corpus, "candidates", drift)
    with pytest.raises(ValueError, match="not verified current"):
        collect_with_receipt(output, policy)
    assert destination(output).read_bytes() == before


def test_receipt_permissions_change_during_evaluation_preserves_result(tmp_path, monkeypatch):
    import recall.voice_corpus as corpus

    inputs, output, _, _, policy = replay_fixture(tmp_path)
    replay(inputs, output)
    collect_with_receipt(output, policy)
    before = destination(output).read_bytes()
    original = corpus.candidates

    def drift(*args, **kwargs):
        report = original(*args, **kwargs)
        manifest(output).chmod(0o644)
        return report

    monkeypatch.setattr(corpus, "candidates", drift)
    with pytest.raises(ValueError, match="not verified current"):
        collect_with_receipt(output, policy)
    assert destination(output).read_bytes() == before
    assert not inspect_day(output, day=DAY)["verified_current"]


def test_empty_telegram_receipt_requires_boolean_capture_day_proof(tmp_path):
    inputs, output, raw, _, policy = replay_fixture(tmp_path)
    write_jsonl(raw, [])
    replay(inputs, output)
    jobs = read_jsonl(manifest(output))
    jobs[0]["capture_day_present"] = "false"
    write_jsonl(manifest(output), jobs)
    with pytest.raises(ValueError, match="not verified current"):
        collect_with_receipt(output, policy)
    assert not destination(output).exists()


def test_replay_opt_in_runs_after_publication_and_failure_is_separate(tmp_path):
    inputs, output, raw, _, policy = replay_fixture(tmp_path)
    assert "voice" not in replay(inputs, output)[0]
    assert not (output.derived / "voice").exists()
    job = replay(inputs, output, voice_policy=policy)[0]
    assert job["status"] == "success" and job["voice"]["verified_current"]
    assert inspect_day(output, day=DAY)["verified_current"]
    before = destination(output).read_bytes()
    write_jsonl(raw, [])
    policy.unlink()
    job = replay(inputs, output, voice_policy=policy)[0]
    assert job["status"] == "captured-empty" and not job["voice"]["verified_current"]
    assert output.normalized_event_path(DAY).read_bytes() == b""
    assert destination(output).read_bytes() == before
    assert "voice" not in read_jsonl(manifest(output))[0]


@pytest.mark.parametrize("status", ["missing", "failed", "unsupported"])
def test_replay_does_not_collect_unpublished_jobs(tmp_path, status):
    inputs, output, raw, _, policy = replay_fixture(tmp_path)
    if status == "missing":
        raw.unlink()
        (inputs.raw_capture_dir("telegram", DAY) / "updates.jsonl").unlink()
        raw.parent.rmdir()
        inputs.raw_capture_dir("telegram", DAY).rmdir()
    elif status == "failed":
        raw.write_bytes(b"not json\n")
    else:
        write_jsonl(raw, [{"payload": {"@type": "unknown"}}])
        # Unsupported status is keyed by capture day, not every event day.
        write_jsonl(
            inputs.raw_capture_dir("telegram", DAY) / "updates.jsonl",
            [{"payload": {"@type": "unknown"}}],
        )
    job = replay(inputs, output, voice_policy=policy)[0]
    assert job["status"] == status and "voice" not in job
    assert not (output.derived / "voice").exists()


@pytest.mark.parametrize(
    "account,sources", [(None, ["telegram"]), (" ", ["telegram"]), ("personal", ["slack"])]
)
def test_replay_opt_in_requires_bounded_supported_scope_before_writes(tmp_path, account, sources):
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    with pytest.raises(ValueError, match="explicit account"):
        rebuild_range(
            inputs,
            output,
            first=DAY,
            last=DAY,
            sources=sources,
            account=account,
            voice_policy=Path("absent"),
        )
    assert not output.root.exists()


def test_collect_receipt_cli_prints_no_sample_text(tmp_path):
    inputs, output, _, _, policy = replay_fixture(tmp_path)
    replay(inputs, output)
    answer = CliRunner().invoke(
        app,
        [
            "voice",
            "collect",
            "--root",
            str(output.root),
            "--date",
            DAY,
            "--source",
            "telegram",
            "--account",
            "personal",
            "--policy",
            str(policy),
            "--coverage",
            str(manifest(output)),
        ],
    )
    assert answer.exit_code == 0, answer.output
    assert json.loads(answer.stdout)["counts"]["eligible"] == 1
    assert result(output)["scopes"][0]["records"][0]["passage"]["text"] not in answer.stdout


def normalizer_fixture(tmp_path, source, monkeypatch):
    paths, raw, row, _, policy_path, policy = setup(tmp_path)
    if source == "telegram":
        raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
        write_jsonl(raw, [row])
        account = "personal"
    else:
        paths, raw, _, event = mail_fixture(paths.root)
        account = "work"
        policy["ownerships"][0].update(
            source="email", account=account, identity=event["sender_identity_id"]
        )
        write_policy(policy_path, policy)
        monkeypatch.setattr(
            "recall.connectors.email.notmuch.run_notmuch_command", lambda args: str(raw) + "\n"
        )
    # Candidate fixtures use a fake email event ID; let the real normalizer publish its ID.
    write_jsonl(paths.normalized_event_path(DAY), [])
    return paths, raw, row, policy_path, account


def normalize_cli(paths, source, account, policy=None):
    args = ["normalize", source, "--root", str(paths.root), "--date", DAY]
    if policy is not None:
        args += ["--voice-policy", str(policy)]
    if source == "email":
        args += ["--account", account]
    elif policy is not None:
        args += ["--voice-account", account]
    return CliRunner().invoke(app, args)


@pytest.mark.parametrize("source", ["email", "telegram"])
def test_normalize_opt_in_after_publication_and_default_unchanged(tmp_path, monkeypatch, source):
    import importlib
    import socket
    import subprocess

    paths, raw, _, policy, account = normalizer_fixture(tmp_path, source, monkeypatch)
    protected = raw.read_bytes(), raw.stat().st_mtime_ns, policy.read_bytes()
    cli = importlib.import_module(f"recall.connectors.{source}.cli")
    original = cli.after_publication
    calls = []

    def check_publication(*args, **kwargs):
        rows = read_jsonl(paths.normalized_event_path(DAY))
        assert any(row["source"] == source for row in rows)
        assert rows[0]["event_id"] != "evt_voice"
        calls.append(kwargs)
        return original(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network or subprocess")

    monkeypatch.setattr(cli, "after_publication", check_publication)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    answer = normalize_cli(paths, source, account)
    assert answer.exit_code == 0, answer.output
    assert "voice=" not in answer.stdout and not calls
    assert not destination(paths).exists()
    answer = normalize_cli(paths, source, account, policy)
    assert answer.exit_code == 0, answer.output
    assert len(calls) == 1
    summary = json.loads(answer.stdout.split("voice=", 1)[1])
    assert summary["counts"]["eligible"] == 1 and summary["verified_current"]
    assert inspect_day(paths, day=DAY)["verified_current"]
    passage = result(paths)["scopes"][0]["records"][0]["passage"]["text"]
    assert passage not in answer.stdout
    assert (raw.read_bytes(), raw.stat().st_mtime_ns, policy.read_bytes()) == protected


@pytest.mark.parametrize("source", ["email", "telegram"])
def test_normalize_voice_failure_preserves_already_published_evidence(
    tmp_path, monkeypatch, source
):
    paths, raw, row, policy, account = normalizer_fixture(tmp_path, source, monkeypatch)
    answer = normalize_cli(paths, source, account, policy)
    assert answer.exit_code == 0, answer.output
    before = destination(paths).read_bytes()
    if source == "telegram":
        row["payload"]["message"]["content"]["text"]["text"] = "I have revised my plan."
        write_jsonl(raw, [row])
    else:
        raw.write_bytes(
            raw.read_bytes().replace(
                b"I checked the figures. They look right to me.", b"I have revised my plan."
            )
        )
    policy.unlink()
    answer = normalize_cli(paths, source, account, policy)
    assert answer.exit_code == 1, answer.output
    assert "Normalized" in answer.stdout and '"verified_current": false' in answer.stdout
    assert read_jsonl(paths.normalized_event_path(DAY))[0]["text"] == "I have revised my plan."
    assert destination(paths).read_bytes() == before
    assert not inspect_day(paths, day=DAY)["verified_current"]


@pytest.mark.parametrize("source", ["email", "telegram"])
def test_normalizer_failure_never_calls_voice_hook(tmp_path, monkeypatch, source):
    import importlib

    paths, _, _, policy, account = normalizer_fixture(tmp_path, source, monkeypatch)
    cli = importlib.import_module(f"recall.connectors.{source}.cli")
    before = paths.normalized_event_path(DAY).read_bytes()
    calls = []

    def failed(*args, **kwargs):
        raise ValueError("Fixture normalization failed")

    monkeypatch.setattr(cli, f"normalize_{source}_day", failed)
    monkeypatch.setattr(cli, "after_publication", lambda *args, **kwargs: calls.append(kwargs))
    answer = normalize_cli(paths, source, account, policy)
    assert answer.exit_code != 0 and not calls
    assert paths.normalized_event_path(DAY).read_bytes() == before
    assert not destination(paths).exists()


@pytest.mark.parametrize(
    "source,extra",
    [
        ("email", []),
        ("email", ["--account", " "]),
        ("telegram", []),
        ("telegram", ["--voice-account", " "]),
    ],
)
def test_normalize_opt_in_requires_explicit_scope_before_writes(tmp_path, source, extra):
    paths = RecallPaths.from_root(tmp_path / "absent")
    answer = CliRunner().invoke(
        app,
        [
            "normalize",
            source,
            "--root",
            str(paths.root),
            "--date",
            DAY,
            "--voice-policy",
            str(tmp_path / "absent-policy"),
            *extra,
        ],
    )
    assert answer.exit_code != 0 and not paths.root.exists()


def test_email_replay_query_receipt_allows_empty_but_keeps_other_sources(tmp_path, monkeypatch):
    inputs, _, _, policy, account = normalizer_fixture(tmp_path / "inputs", "email", monkeypatch)
    output = RecallPaths.from_root(tmp_path / "output")
    write_jsonl(
        output.normalized_event_path(DAY),
        [
            {
                "event_id": "evt_other",
                "source": "slack",
                "kind": "message",
                "account": "team",
                "date": DAY,
                "timestamp": DAY + "T12:00:00Z",
            }
        ],
    )

    def replay_email():
        return rebuild_range(
            inputs,
            output,
            first=DAY,
            last=DAY,
            sources=["email"],
            account=account,
            voice_policy=policy,
        )[0]

    job = replay_email()
    assert job["status"] == "success" and job["voice"]["verified_current"]
    assert job["voice"]["counts"]["eligible"] == 1
    assert inspect_day(output, day=DAY)["verified_current"]
    monkeypatch.setattr("recall.connectors.email.notmuch.run_notmuch_command", lambda args: "")
    job = replay_email()
    assert job["status"] == "queried-empty" and job["voice"]["verified_current"]
    assert result(output)["scopes"][0]["records"] == []
    assert read_jsonl(output.normalized_event_path(DAY))[0]["event_id"] == "evt_other"
    assert inspect_day(output, day=DAY)["verified_current"]
    snapshot = output.state / f"rebuild/email-{DAY}-events.jsonl"
    snapshot.write_bytes(b"{}\n")
    assert not inspect_day(output, day=DAY)["verified_current"]


def test_rebuild_cli_voice_failure_exit_does_not_undo_publication(tmp_path):
    inputs, output, _, _, policy = replay_fixture(tmp_path)
    policy.unlink()
    answer = CliRunner().invoke(
        app,
        [
            "rebuild",
            "--root",
            str(inputs.root),
            "--output-root",
            str(output.root),
            "--from",
            DAY,
            "--to",
            DAY,
            "--source",
            "telegram",
            "--account",
            "personal",
            "--voice-policy",
            str(policy),
        ],
    )
    assert answer.exit_code == 1, answer.output
    assert "telegram: success" in answer.stdout and '"verified_current": false' in answer.stdout
    assert read_jsonl(output.normalized_event_path(DAY))
    assert read_jsonl(manifest(output))[0]["status"] == "success"
    assert "voice" not in read_jsonl(manifest(output))[0]
    assert not destination(output).exists()
