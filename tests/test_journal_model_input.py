import json
import socket
import sqlite3
import subprocess

import pytest
from test_journals import DAY, TZ, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.synthesize import journal


def packet(tmp_path):
    paths = workspace(tmp_path / "inputs")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0].update(
        text="Current reply.\nOn Tuesday Pat wrote:\n> Earlier request, not today's completion.",
        sender_identity_id="ident_example",
        source_urls=["https://example.org/?tracking=PRIVATE_TRANSPORT"],
        raw_ref={"path": "PRIVATE_PHYSICAL_PATH", "locator": {"message_id": "fixture"}},
        raw_fragment={"subject": "Useful subject", "headers": {"From": "Example"}},
        tags=["email"],
    )
    write_jsonl(paths.normalized_event_path(DAY), rows)
    return journal.prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)


def command(directory, *args):
    return CliRunner().invoke(app, ["journal", "input-view", "--packet", str(directory), *args])


def test_versioned_view_keeps_full_text_attribution_and_citation_ids(tmp_path):
    directory = packet(tmp_path)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    text, summary = journal.model_input(directory)
    view = json.loads(text)
    assert view["format"] == "recall-journal-model-input-v1"
    assert summary["events"] == summary["source_events"] == 2
    assert summary["bytes"] == len(text.encode()) <= summary["max_bytes"]
    assert view["omitted_event_ids"] == []
    assert view["omitted_event_fields"] == ["raw_ref", "source_urls"]
    assert "PRIVATE_TRANSPORT" not in text and "PRIVATE_PHYSICAL_PATH" not in text
    assert view["allowed_citation_markers"] == ["[fn:evt_break]", "[fn:evt_mail]"]
    originals = {row["event_id"]: row for row in journal._load_packet(directory)[1]}
    for row in view["events"]:
        assert row == {
            key: value
            for key, value in originals[row["event_id"]].items()
            if key not in {"raw_ref", "source_urls"}
        }
    assert "Earlier request" in text and "Useful subject" in text
    assert journal.model_input(directory) == (text, summary)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()
    } == before


def test_selection_is_explicit_and_never_silently_drops_or_reorders(tmp_path):
    directory = packet(tmp_path)
    text, summary = journal.model_input(directory, event_ids=["evt_mail"])
    view = json.loads(text)
    assert [row["event_id"] for row in view["events"]] == ["evt_mail"]
    assert view["omitted_event_ids"] == ["evt_break"]
    assert view["allowed_citation_markers"] == ["[fn:evt_mail]"]
    assert summary["omitted_events"] == 1
    for values in (["unknown"], ["evt_mail", "evt_mail"]):
        with pytest.raises(ValueError, match="event"):
            journal.model_input(directory, event_ids=values)


def test_budget_uses_encoded_bytes_and_rejects_instead_of_truncating(tmp_path):
    directory = packet(tmp_path)
    text, summary = journal.model_input(directory)
    with pytest.raises(ValueError, match="budget"):
        journal.model_input(directory, max_bytes=summary["bytes"] - 1)
    assert journal.model_input(directory, max_bytes=summary["bytes"])[0] == text
    for value in (0, -1, True):
        with pytest.raises(ValueError, match="budget"):
            journal.model_input(directory, max_bytes=value)


def test_corrupt_packet_refuses_view(tmp_path):
    directory = packet(tmp_path)
    (directory / "events.jsonl").write_text("changed\n")
    with pytest.raises(ValueError, match="changed"):
        journal.model_input(directory)


def test_cli_preview_is_read_only_and_prints_no_private_prose(tmp_path, monkeypatch):
    directory = packet(tmp_path)
    before = {path: path.read_bytes() for path in directory.parents[5].rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected side effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = command(directory)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["events"] == 2
    assert "Current reply" not in result.stdout and "Earlier request" not in result.stdout
    assert {path: path.read_bytes() for path in before} == before


def test_private_export_retry_and_existing_file_preservation(tmp_path):
    directory = packet(tmp_path)
    output = tmp_path / "private/view.json"
    result = command(directory, "--output", str(output))
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_bytes())["format"] == "recall-journal-model-input-v1"
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    before = output.read_bytes(), output.stat().st_mtime_ns
    assert command(directory, "--output", str(output)).exit_code == 0
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before
    output.write_text("human-owned contents")
    assert command(directory, "--output", str(output)).exit_code != 0
    assert output.read_text() == "human-owned contents"


@pytest.mark.parametrize("kind", ["public_parent", "public_file", "symlink", "packet", "budget"])
def test_export_rejects_unsafe_destinations_or_over_budget_before_writing(tmp_path, kind):
    directory = packet(tmp_path)
    output = tmp_path / "private/view.json"
    args = []
    if kind == "public_parent":
        output.parent.mkdir(mode=0o755)
        output.parent.chmod(0o755)
    elif kind == "public_file":
        output.parent.mkdir(mode=0o700)
        output.write_text("original")
        output.chmod(0o644)
    elif kind == "symlink":
        output.parent.mkdir(mode=0o700)
        target = tmp_path / "target"
        target.write_text("original")
        output.symlink_to(target)
    elif kind == "packet":
        output = directory / "view.json"
    else:
        args = ["--max-bytes", "1"]
    result = command(directory, "--output", str(output), *args)
    assert result.exit_code != 0
    if kind in {"packet", "budget"}:
        assert not output.exists()


def test_export_cannot_enter_evidence_data_through_parent_alias(tmp_path):
    directory = packet(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    result = command(directory, "--output", str(alias / "view.json"))
    assert result.exit_code != 0
    assert not (directory / "view.json").exists()


def test_export_lock_and_atomic_failure_preserve_existing_output(tmp_path, monkeypatch):
    import fcntl

    directory = packet(tmp_path)
    output = tmp_path / "private/view.json"
    assert command(directory, "--output", str(output)).exit_code == 0
    before = output.read_bytes(), output.stat().st_mtime_ns
    with (output.parent / ".journal-input-view.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert command(directory, "--output", str(output)).exit_code != 0
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before

    def failed(*args, **kwargs):
        raise OSError("Fixture atomic-write failure")

    monkeypatch.setattr(journal, "write_text_atomic", failed)
    new_output = output.with_name("new.json")
    assert command(directory, "--output", str(new_output)).exit_code != 0
    assert not new_output.exists()
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before


def test_packet_drift_during_projection_refuses_view(tmp_path, monkeypatch):
    directory = packet(tmp_path)
    original = journal._json

    def drift(value):
        result = original(value)
        with (directory / "prompt.org").open("a") as handle:
            handle.write("changed")
        return result

    monkeypatch.setattr(journal, "_json", drift)
    with pytest.raises(ValueError, match="changed during"):
        journal.model_input(directory)
