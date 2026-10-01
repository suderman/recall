import fcntl
import json
from pathlib import Path

import pytest
from test_journals import DAY, TZ, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.synthesize import generate
from recall.synthesize.journal import prepare_journal, save_journal


def saved_revision(paths):
    packet = prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    prompt = generate._prompt(packet)
    return save_journal(
        paths,
        packet_dir=packet,
        body="I prepared the update.[fn:evt_mail]",
        model=generate.DEFAULT_MODEL,
        generation_options={
            "runner": "pi-json-cli-v1",
            "prompt_sha256": generate._sha(prompt),
            "system_sha256": generate._sha(generate.SYSTEM_PROMPT),
            "responseId": "actual-original-response",
            "tools_enabled": False,
        },
    )


def no_model(*args):
    raise AssertionError("Publication must not call a model")


def test_reviewed_publication_preserves_original_and_cached_build(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    originals = {path: path.read_bytes() for path in paths.derived.rglob("*") if path.is_file()}
    normalized = paths.normalized_event_path(DAY).read_bytes()
    monkeypatch.setattr(generate, "run_pi", no_model)
    output = tmp_path / "journal"
    draft = "I sent the requested update.[fn:evt_mail]"
    result = generate.publish_journal(paths, revision=revision, output=output, draft=draft)
    target = Path(result["path"])
    reviewed = Path(result["revision"])
    assert target.read_bytes() == reviewed.read_bytes()
    assert "sent the requested" in target.read_text()
    assert reviewed != revision
    record = json.loads((reviewed.parent / "generation.json").read_text())
    assert record["generation_options"]["responseId"] == "actual-original-response"
    assert record["generation_options"]["review"]["original_revision"] == str(revision)
    assert record["generation_options"]["review"]["original_body_sha256"] == generate._sha(
        "I prepared the update.[fn:evt_mail]"
    )
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert paths.normalized_event_path(DAY).read_bytes() == normalized
    assert generate.publish_journal(paths, revision=revision, output=output, draft=draft) == result
    assert (
        generate.build_journals(
            paths, first=DAY, last=DAY, author="Example", timezone_name=TZ, output=output
        )[0]["status"]
        == "cached"
    )
    assert target.read_bytes() == reviewed.read_bytes()
    # Frozen publication does not claim unchanged live evidence. A build must notice changes.
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["text"] += " New detail."
    write_jsonl(paths.normalized_event_path(DAY), rows)
    with pytest.raises(AssertionError, match="must not call"):
        generate.build_journals(
            paths, first=DAY, last=DAY, author="Example", timezone_name=TZ, output=output
        )
    assert target.read_bytes() == reviewed.read_bytes()


@pytest.mark.parametrize("name", ["journal.org", "body.org", "generation.json"])
def test_publication_refuses_edited_revision(tmp_path, monkeypatch, name):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    altered = revision.parent / name
    altered.write_text(altered.read_text() + " Changed")
    monkeypatch.setattr(generate, "run_pi", no_model)
    with pytest.raises(ValueError, match="edited|Invalid journal revision"):
        generate.publish_journal(paths, revision=revision, output=tmp_path / "journal")
    assert not (tmp_path / "journal").exists()
    assert not (paths.state / "journal-builds.jsonl").exists()


@pytest.mark.parametrize("name", ["events.jsonl", "prompt.org"])
def test_publication_refuses_edited_packet(tmp_path, name):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    record = json.loads((revision.parent / "generation.json").read_text())
    altered = Path(record["packet"]) / name
    altered.write_text(altered.read_text() + " Changed")
    with pytest.raises(ValueError, match="packet was changed"):
        generate.publish_journal(paths, revision=revision, output=tmp_path / "journal")
    assert not (tmp_path / "journal").exists()


def test_manual_edits_busy_writer_and_unsafe_draft(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    output = tmp_path / "journal"
    monkeypatch.setattr(generate, "run_pi", no_model)
    result = generate.publish_journal(paths, revision=revision, output=output)
    target = Path(result["path"])
    old_manifest = (paths.state / "journal-builds.jsonl").read_bytes()
    target.write_text("My own notes")
    with pytest.raises(ValueError, match="handwritten/edited"):
        generate.publish_journal(paths, revision=revision, output=output, draft="Bad.[fn:unknown]")
    assert target.read_text() == "My own notes"
    assert (paths.state / "journal-builds.jsonl").read_bytes() == old_manifest
    target.write_bytes(revision.read_bytes())
    with (paths.state / "journal-build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            generate.publish_journal(paths, revision=revision, output=output)
    for draft in ("Bad.[fn:unknown]", "#+begin_src emacs-lisp\nBad.[fn:evt_mail]"):
        with pytest.raises(ValueError):
            generate.publish_journal(paths, revision=revision, output=output, draft=draft)
    assert target.read_bytes() == revision.read_bytes()
    assert (paths.state / "journal-builds.jsonl").read_bytes() == old_manifest


@pytest.mark.parametrize("stage", ["before_replace", "after_replace"])
def test_failed_publication_can_resume_replacing_pending_revision(tmp_path, monkeypatch, stage):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    output = tmp_path / "journal"
    original_write = generate.write_jsonl

    def interrupted(path, rows):
        if path.name == "journal-publications.jsonl":
            raise KeyboardInterrupt()
        return original_write(path, rows)

    monkeypatch.setattr(generate, "write_jsonl", interrupted)
    with pytest.raises(KeyboardInterrupt):
        generate.publish_journal(paths, revision=revision, output=output)
    monkeypatch.setattr(generate, "write_jsonl", original_write)
    target = output / "2026/03" / f"{DAY}.org"
    assert target.read_bytes() == revision.read_bytes()
    assert not (paths.state / "journal-publications.jsonl").exists()
    original_text = generate.write_text_atomic
    if stage == "before_replace":
        monkeypatch.setattr(
            generate, "write_text_atomic", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
        )
    else:
        monkeypatch.setattr(generate, "write_jsonl", interrupted)
    draft = "Reviewed the update.[fn:evt_mail]"
    with pytest.raises((OSError, KeyboardInterrupt)):
        generate.publish_journal(paths, revision=revision, output=output, draft=draft)
    monkeypatch.setattr(generate, "write_text_atomic", original_text)
    monkeypatch.setattr(generate, "write_jsonl", original_write)
    monkeypatch.setattr(generate, "run_pi", no_model)
    result = generate.publish_journal(paths, revision=revision, output=output, draft=draft)
    assert target.read_bytes() == Path(result["revision"]).read_bytes()
    assert (
        generate.build_journals(
            paths, first=DAY, last=DAY, author="Example", timezone_name=TZ, output=output
        )[0]["status"]
        == "cached"
    )


def test_edit_during_checkpoint_refuses_replacement(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    output = tmp_path / "journal"
    result = generate.publish_journal(paths, revision=revision, output=output)
    target = Path(result["path"])
    original_write = generate.write_jsonl

    def human_edit(path, rows):
        original_write(path, rows)
        if path.name == "journal-builds.jsonl":
            target.write_text("Human edit during publication")

    monkeypatch.setattr(generate, "write_jsonl", human_edit)
    with pytest.raises(ValueError, match="handwritten/edited"):
        generate.publish_journal(
            paths, revision=revision, output=output, draft="Revised.[fn:evt_mail]"
        )
    assert target.read_text() == "Human edit during publication"


def test_revision_edit_before_publication_is_not_adopted(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    original_publish = generate._publish

    def tampered(*args):
        args[2].write_text("Changed after validation")
        return original_publish(*args)

    monkeypatch.setattr(generate, "_publish", tampered)
    with pytest.raises(ValueError, match="edited"):
        generate.publish_journal(paths, revision=revision, output=tmp_path / "journal")
    assert not (tmp_path / "journal").exists()
    assert not (paths.state / "journal-builds.jsonl").exists()


def test_external_revision_does_not_inherit_native_cache(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    output = tmp_path / "journal"
    generate.publish_journal(paths, revision=revision, output=output)
    packet = Path(json.loads((revision.parent / "generation.json").read_text())["packet"])
    external = save_journal(
        paths, packet_dir=packet, body="External draft.[fn:evt_mail]", model="external-model"
    )
    result = generate.publish_journal(paths, revision=external, output=output)
    assert "External draft" in Path(result["path"]).read_text()
    monkeypatch.setattr(generate, "run_pi", no_model)
    with pytest.raises(AssertionError, match="must not call"):
        generate.build_journals(
            paths, first=DAY, last=DAY, author="Example", timezone_name=TZ, output=output
        )
    assert "External draft" in Path(result["path"]).read_text()


def test_foreign_revision_source_output_and_symlink_refusal(tmp_path):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    with pytest.raises(ValueError, match="this workspace"):
        generate.publish_journal(
            workspace(tmp_path / "other"), revision=revision, output=tmp_path / "journal"
        )
    for output in (paths.raw, paths.normalized, paths.state, paths.derived):
        with pytest.raises(ValueError, match="outside Recall data"):
            generate.publish_journal(paths, revision=revision, output=output)
    output = tmp_path / "journal"
    target = output / "2026/03" / f"{DAY}.org"
    target.parent.mkdir(parents=True)
    other_file = tmp_path / "other.org"
    other_file.write_bytes(revision.read_bytes())
    target.symlink_to(other_file)
    with pytest.raises(ValueError, match="symlink"):
        generate.publish_journal(paths, revision=revision, output=output)
    assert target.is_symlink()


def test_cli_publish_corrected_body_and_manual_refusal(tmp_path, monkeypatch):
    paths = workspace(tmp_path / "recall")
    revision = saved_revision(paths)
    output = tmp_path / "journal"
    draft = tmp_path / "reviewed.org"
    draft.write_text("Reviewed the update.[fn:evt_mail]")
    monkeypatch.setattr(generate, "run_pi", no_model)
    args = [
        "journal",
        "publish",
        "--root",
        str(paths.root),
        "--revision",
        str(revision),
        "--output",
        str(output),
        "--draft",
        str(draft),
    ]
    cli = CliRunner()
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "published" in result.output and "Revision:" in result.output
    target = output / "2026/03" / f"{DAY}.org"
    target.write_text("Keep my notes")
    result = cli.invoke(app, args)
    assert result.exit_code == 1 and "handwritten/edited" in result.output
    assert target.read_text() == "Keep my notes"
