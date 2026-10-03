from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.search import build_index, render_results, search
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.references import raw_reference
from recall.synthesize.journal import prepare_journal

MAIL = b"Message-ID: <fixture@example.test>\nSubject: Retained mail\n\nOriginal body\n"


def fixture(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "archive")
    raw = tmp_path / "mail/Inbox/cur/unique[1],U=12:2,S"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(MAIL)
    event = {
        "event_id": "evt_mail",
        "date": "2026-09-30",
        "source": "email",
        "kind": "email",
        "timestamp": "2026-09-30T10:00:00Z",
        "text": "Retained mail",
        "raw_ref": {
            "source": "email",
            "path": str(raw),
            "locator": {"message_id": "fixture@example.test"},
        },
    }
    normalized = paths.normalized_event_path(event["date"])
    write_jsonl(normalized, [event])
    return paths, raw, normalized, event


def test_flag_rename_resolves_search_and_packet_without_writes(tmp_path):
    paths, raw, normalized, event = fixture(tmp_path)
    index = Path(build_index([paths])["index"])
    packet = prepare_journal(paths, day=event["date"], author="Fixture")
    renamed = raw.with_name("unique[1],U=12:2,RS")
    raw.rename(renamed)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = search(index)[0]
    assert result["event"] == event
    assert result["resolved_raw_path"] == str(renamed)
    assert result["raw_citation_error"] is None
    assert str(renamed) in unquote(render_results([result], org=True))
    inspected = CliRunner().invoke(app, ["journal", "inspect", "--packet", str(packet)])
    assert inspected.exit_code == 0, inspected.output
    data = json.loads(inspected.output)
    assert data["unresolved_citations"] == []
    assert data["citations"][0]["resolved_raw_path"] == str(renamed)
    assert {p: p.read_bytes() for p in before} == before
    assert not raw.exists() and renamed.read_bytes() == MAIL


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "ambiguous",
        "wrong-id",
        "missing-id",
        "duplicate-id",
        "no-locator",
        "symlink",
        "directory",
        "other-stem",
        "other-folder",
        "non-maildir",
        "not-email",
    ],
)
def test_unverifiable_renames_remain_visible(tmp_path, damage):
    paths, raw, normalized, event = fixture(tmp_path)
    packet = prepare_journal(paths, day=event["date"], author="Fixture")
    renamed = raw.with_name("unique[1],U=12:2,RS")
    raw.rename(renamed)
    if damage == "missing":
        renamed.unlink()
    elif damage == "ambiguous":
        raw.with_name("unique[1],U=12:2,F").write_bytes(MAIL)
    elif damage == "wrong-id":
        renamed.write_bytes(MAIL.replace(b"fixture@example.test", b"different@example.test"))
    elif damage == "missing-id":
        renamed.write_bytes(b"Subject: No ID\n\nBody\n")
    elif damage == "duplicate-id":
        renamed.write_bytes(b"Message-ID: <fixture@example.test>\n" + MAIL)
    elif damage == "no-locator":
        event["raw_ref"]["locator"] = {}
    elif damage == "symlink":
        renamed.unlink()
        target = tmp_path / "outside.eml"
        target.write_bytes(MAIL)
        renamed.symlink_to(target)
    elif damage == "directory":
        renamed.unlink()
        renamed.mkdir()
    elif damage == "other-stem":
        renamed.rename(renamed.with_name("unique[1],U=123:2,RS"))
    elif damage == "other-folder":
        renamed.rename(tmp_path / "moved.eml")
    elif damage == "non-maildir":
        event["raw_ref"]["path"] = str(raw.parent.parent / raw.name)
    elif damage == "not-email":
        event["source"] = "telegram"
    path, error = raw_reference(event, normalized)
    assert path == raw or damage == "non-maildir"
    assert error
    if damage == "ambiguous":
        assert "ambiguous" in error.lower()
    write_jsonl(normalized, [event])
    index = Path(build_index([paths])["index"])
    result = search(index)[0]
    assert result["raw_citation_error"]
    rendered = render_results([result], org=True)
    assert "Unresolved raw citation" in rendered
    assert f"[[file:{renamed}" not in unquote(rendered)
    if damage not in {"no-locator", "non-maildir", "not-email"}:
        inspected = CliRunner().invoke(app, ["journal", "inspect", "--packet", str(packet)])
        assert inspected.exit_code == 1, inspected.output
        assert json.loads(inspected.output)["unresolved_citations"][0]["raw_citation_error"]


def test_exact_path_wins_and_mapping_precedes_flag_lookup(tmp_path, monkeypatch):
    paths, raw, normalized, event = fixture(tmp_path)
    raw.with_name("unique[1],U=12:2,F").write_bytes(MAIL)
    assert raw_reference(event, normalized) == (raw, None)
    retained = tmp_path / "restore/cur"
    retained.mkdir(parents=True)
    renamed = retained / "unique[1],U=12:2,RS"
    renamed.write_bytes(MAIL)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(raw.parent), "new": str(retained)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    assert raw_reference(event, normalized) == (renamed, None)
    renamed.unlink()
    # Existing original mail must not mask a missing authoritative restore.
    assert raw_reference(event, normalized)[1]


def test_unreadable_renamed_file_is_not_a_valid_link(tmp_path, monkeypatch):
    paths, raw, normalized, event = fixture(tmp_path)
    renamed = raw.with_name("unique[1],U=12:2,RS")
    raw.rename(renamed)
    original = Path.open

    def failed(path, *args, **kwargs):
        if path == renamed:
            raise OSError("injected read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed)
    assert raw_reference(event, normalized)[1]


def test_relative_maildir_reference_and_repeated_flag_changes(tmp_path):
    paths, raw, normalized, event = fixture(tmp_path)
    destination = paths.raw / "mail/cur" / raw.name
    destination.parent.mkdir(parents=True)
    raw.rename(destination)
    event["raw_ref"]["path"] = str(destination.relative_to(paths.root))
    first = destination.with_name("unique[1],U=12:2,RS")
    destination.rename(first)
    assert raw_reference(event, normalized) == (first, None)
    second = first.with_name("unique[1],U=12:2,FRS")
    first.rename(second)
    assert raw_reference(event, normalized) == (second, None)
    assert event["raw_ref"]["path"].endswith(":2,S")


def test_same_message_id_with_different_filename_is_not_recovered(tmp_path):
    paths, raw, normalized, event = fixture(tmp_path)
    raw.rename(raw.with_name("unrelated:2,RS"))
    assert raw_reference(event, normalized)[1] == "Maildir citation missing"
