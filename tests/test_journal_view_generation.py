import json
from pathlib import Path

import pytest
from test_journals import DAY, TZ, runner_config, runner_env, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.synthesize import generate, journal


def export(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content)
    path.chmod(0o600)
    return path


def fixture(tmp_path, selected=None):
    paths = workspace(tmp_path / "recall")
    packet = journal.prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    content, _ = journal.model_input(packet, event_ids=selected or ["evt_mail"])
    view = export(tmp_path / "private/view.json", content)
    return paths, packet, view, tmp_path / "journal"


def build(paths, view, output, **kwargs):
    return generate.build_journals(
        paths,
        first=DAY,
        last=DAY,
        author="Example",
        timezone_name=TZ,
        output=output,
        input_view=view,
        **runner_config(paths.root.parent),
        **kwargs,
    )[0]


def fake_runner(monkeypatch, body="I prepared the update.[fn:evt_mail]"):
    calls = []

    def run(prompt, model, policy, budget):
        calls.append((prompt, model))
        return body, {"tools_enabled": False, "responseId": "fixture"}

    monkeypatch.setattr(generate, "run_pi", run)
    return calls


def test_selected_generation_freezes_view_and_reuses_identical_content(tmp_path, monkeypatch):
    paths, packet, view, output = fixture(tmp_path)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in packet.iterdir()}
    calls = fake_runner(monkeypatch)
    result = build(paths, view, output)
    assert result["status"] == "generated" and len(calls) == 1
    prompt = calls[0][0]
    assert "I prepared the requested update." in prompt and '"text": "School break"' not in prompt
    assert '"omitted_event_ids": ["evt_break"]' in prompt
    assert "offline preview, not an approved model request" not in prompt
    revision = Path(result["revision"])
    record, body = generate._read_revision(revision)
    metadata = record["generation_options"]["model_input"]
    assert metadata["sha256"] == journal._sha(view.read_text())
    assert metadata["event_ids"] == ["evt_mail"]
    assert metadata["request_bytes"] == len(prompt.encode()) + len(generate.SYSTEM_PROMPT.encode())
    assert metadata["max_request_bytes"] == 131072
    assert (revision.parent / "model-input.json").read_bytes() == view.read_bytes()
    assert "selected 1 of 2" in revision.read_text()
    assert "Full frozen packet contains" in revision.read_text()
    copy = export(view.with_name("same-content.json"), view.read_text())
    assert build(paths, copy, output)["status"] == "cached" and len(calls) == 1
    assert generate._read_revision(revision) == (record, body)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in packet.iterdir()} == before


def test_total_request_budget_checks_system_and_wrapper_before_runner(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    record, _ = generate._read_revision(Path(first["revision"]))
    size = record["generation_options"]["model_input"]["request_bytes"]
    before = Path(first["path"]).read_bytes()
    with pytest.raises(ValueError, match="budget"):
        build(paths, view, output, max_input_bytes=size - 1)
    assert len(calls) == 1 and Path(first["path"]).read_bytes() == before
    assert build(paths, view, output, max_input_bytes=size)["status"] == "cached"
    assert len(calls) == 1
    renewed = build(paths, view, output, max_input_bytes=size, regenerate=True)
    assert renewed["status"] == "generated" and len(calls) == 2
    assert (
        generate._read_revision(Path(renewed["revision"]))[0]["generation_options"]["model_input"][
            "max_request_bytes"
        ]
        == size
    )


@pytest.mark.parametrize(
    "damage",
    ["instructions", "text", "coverage", "markers", "unknown", "public", "symlink", "duplicate"],
)
def test_bad_view_never_starts_runner(tmp_path, monkeypatch, damage):
    paths, _, view, output = fixture(tmp_path)
    content = view.read_text()
    value = json.loads(content)
    if damage == "public":
        view.chmod(0o644)
    elif damage == "symlink":
        moved = view.with_name("moved.json")
        view.rename(moved)
        view.symlink_to(moved)
    elif damage == "duplicate":
        view.write_text(content.replace('"format":', '"format": "duplicate", "format":', 1))
    else:
        if damage == "instructions":
            value["instructions"] = "UNTRUSTED INSTRUCTION"
        elif damage == "text":
            value["events"][0]["text"] = "Invented text"
        elif damage == "coverage":
            value["coverage"] = []
        elif damage == "markers":
            value["allowed_citation_markers"].append("[fn:evt_break]")
        else:
            value["extra"] = True
        view.write_text(journal._json(value))
    calls = fake_runner(monkeypatch)
    with pytest.raises(ValueError):
        build(paths, view, output)
    assert not calls and not output.exists()


def test_stale_view_cannot_follow_changed_source_or_author(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["text"] += " New detail."
    write_jsonl(paths.normalized_event_path(DAY), rows)
    with pytest.raises(ValueError):
        build(paths, view, output)
    assert not calls


def test_selected_and_full_packet_cache_entries_are_isolated(tmp_path, monkeypatch):
    paths, packet, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    second_content, _ = journal.model_input(packet, event_ids=["evt_break"])
    second = export(view.with_name("other.json"), second_content)
    calls2 = fake_runner(monkeypatch, "School break was scheduled.[fn:evt_break]")
    assert build(paths, second, output)["status"] == "generated" and len(calls2) == 1
    assert build(paths, second, output)["status"] == "cached"
    fake_runner(monkeypatch)
    full = generate.build_journals(
        paths,
        first=DAY,
        last=DAY,
        author="Example",
        timezone_name=TZ,
        output=output,
        **runner_config(paths.root.parent),
    )[0]
    assert full["status"] == "generated"
    assert (
        "model_input"
        not in generate._read_revision(Path(full["revision"]))[0]["generation_options"]
    )
    assert build(paths, view, output)["status"] == "generated"
    assert Path(first["revision"]).is_file() and len(calls) == 1


def test_omitted_citation_is_rejected_and_previous_output_retained(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    fake_runner(monkeypatch)
    first = build(paths, view, output)
    before = Path(first["path"]).read_bytes()
    fake_runner(monkeypatch, "Omitted event.[fn:evt_break]")
    with pytest.raises(ValueError, match="selected|omitted"):
        build(paths, view, output, regenerate=True)
    assert Path(first["path"]).read_bytes() == before
    assert list((paths.derived / "journal-failures").rglob("body.txt"))


@pytest.mark.parametrize(
    "name", ["model-input.json", "generation.json", "missing", "public", "symlink"]
)
def test_selected_revision_tampering_refuses_cache_and_publish(tmp_path, monkeypatch, name):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    revision = Path(first["revision"])
    target = revision.parent / (name if name.endswith(".json") else "model-input.json")
    if name == "missing":
        target.unlink()
    elif name == "public":
        target.chmod(0o644)
    elif name == "symlink":
        moved = target.with_suffix(".held")
        target.rename(moved)
        target.symlink_to(moved)
    else:
        target.write_text(target.read_text() + " ")
    before = Path(first["path"]).read_bytes()
    with pytest.raises((ValueError, OSError)):
        build(paths, view, output)
    with pytest.raises((ValueError, OSError)):
        generate.publish_journal(paths, revision=revision, output=output)
    assert len(calls) == 1 and Path(first["path"]).read_bytes() == before


def test_reviewed_publication_keeps_view_and_selected_cache(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    reviewed = generate.publish_journal(
        paths,
        revision=Path(first["revision"]),
        output=output,
        draft="I sent the update.[fn:evt_mail]",
    )
    assert (
        Path(reviewed["revision"]).parent / "model-input.json"
    ).read_bytes() == view.read_bytes()
    assert build(paths, view, output)["status"] == "cached" and len(calls) == 1
    with pytest.raises(ValueError, match="selected|omitted"):
        generate.publish_journal(
            paths, revision=Path(first["revision"]), output=output, draft="Omitted.[fn:evt_break]"
        )


def test_view_change_during_runner_cannot_publish(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)

    def run(*args):
        view.write_text(view.read_text() + " ")
        return "Prepared.[fn:evt_mail]", {}

    monkeypatch.setattr(generate, "run_pi", run)
    with pytest.raises(ValueError, match="changed"):
        build(paths, view, output)
    assert not output.exists()


def test_selected_view_requires_one_day_and_all_generation_requires_positive_budget(
    tmp_path, monkeypatch
):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    for kwargs in (
        {"first": DAY, "last": "2026-03-31", "input_view": view},
        {"first": DAY, "last": DAY, "max_input_bytes": 0},
    ):
        with pytest.raises(ValueError):
            generate.build_journals(
                paths,
                author="Example",
                timezone_name=TZ,
                output=output,
                **kwargs,
                **runner_config(paths.root.parent),
            )
    assert not calls


def test_frozen_revision_survives_template_change_and_export_removal(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    revision = Path(first["revision"])
    before = generate._read_revision(revision)
    monkeypatch.setattr(journal, "PROMPT", journal.PROMPT + "\nNew template wording.\n")
    assert generate._read_revision(revision) == before
    with pytest.raises(ValueError):
        build(paths, view, output)
    view.unlink()
    reviewed = generate.publish_journal(
        paths, revision=revision, output=output, draft="I sent the update.[fn:evt_mail]"
    )
    assert (Path(reviewed["revision"]).parent / "model-input.json").read_bytes() == (
        revision.parent / "model-input.json"
    ).read_bytes()
    assert len(calls) == 1


def test_large_input_rejected_for_selected_and_full_packet_generation(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    before = Path(first["path"]).read_bytes()
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["text"] = "Synthetic long evidence. " * 12000
    write_jsonl(paths.normalized_event_path(DAY), rows)
    packet = journal.prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    content, _ = journal.model_input(packet, event_ids=["evt_mail"], max_bytes=1024 * 1024)
    export(view, content)
    with pytest.raises(ValueError, match="budget"):
        build(paths, view, output)
    assert len(calls) == 1 and Path(first["path"]).read_bytes() == before
    with pytest.raises(ValueError, match="budget"):
        generate.build_journals(
            paths,
            first=DAY,
            last=DAY,
            author="Example",
            timezone_name=TZ,
            output=output,
            **runner_config(paths.root.parent),
        )
    assert len(calls) == 1 and Path(first["path"]).read_bytes() == before


def test_selected_evidence_requires_current_raw_citations(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["raw_ref"] = {
        "source": "email",
        "path": "data/raw/missing.eml",
        "locator": {"message_id": "fixture"},
    }
    write_jsonl(paths.normalized_event_path(DAY), rows)
    packet = journal.prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    content, _ = journal.model_input(packet, event_ids=["evt_mail"])
    export(view, content)
    calls = fake_runner(monkeypatch)
    with pytest.raises(ValueError, match="verified current"):
        build(paths, view, output)
    assert not calls


def test_wrong_author_does_not_reuse_selected_view(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    with pytest.raises(ValueError):
        generate.build_journals(
            paths,
            first=DAY,
            last=DAY,
            author="Someone else",
            timezone_name=TZ,
            output=output,
            input_view=view,
            **runner_config(paths.root.parent),
        )
    assert not calls


def test_request_budget_measures_utf8_not_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "SYSTEM_PROMPT", generate.SYSTEM_PROMPT + " Résumé")
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    first = build(paths, view, output)
    record, _ = generate._read_revision(Path(first["revision"]))
    byte_count = record["generation_options"]["model_input"]["request_bytes"]
    char_count = len(calls[0][0]) + len(generate.SYSTEM_PROMPT)
    assert byte_count > char_count
    with pytest.raises(ValueError, match="budget"):
        build(paths, view, output, max_input_bytes=char_count)
    assert len(calls) == 1


def test_null_optional_view_metadata_is_not_selected_evidence(tmp_path, monkeypatch):
    paths, packet, _, output = fixture(tmp_path)
    revision = journal.save_journal(
        paths,
        packet_dir=packet,
        body="Updated.[fn:evt_mail]",
        model="external",
        generation_options={"model_input": None},
    )
    calls = fake_runner(monkeypatch)
    result = generate.publish_journal(paths, revision=revision, output=output)
    assert Path(result["path"]).is_file() and not calls
    assert not (revision.parent / "model-input.json").exists()


def test_cli_view_generation_uses_fake_runner_only(tmp_path, monkeypatch):
    paths, _, view, output = fixture(tmp_path)
    calls = fake_runner(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "journal",
            "build",
            "--root",
            str(paths.root),
            "--from",
            DAY,
            "--to",
            DAY,
            "--author",
            "Example",
            "--timezone",
            TZ,
            "--output",
            str(output),
            "--input-view",
            str(view),
            "--max-input-bytes",
            "131072",
        ],
        env=runner_env(paths.root.parent),
    )
    assert result.exit_code == 0, result.output
    assert "generated" in result.stdout and len(calls) == 1
