from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.search import build_index, render_results, search
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.references import raw_reference, resolve_reference
from recall.synthesize.generate import _read_revision
from recall.synthesize.journal import _load_packet, prepare_journal, save_journal


def archive(tmp_path):
    old = RecallPaths.from_root(tmp_path / "old")
    raw = old.raw / "fixture.jsonl"
    write_jsonl(raw, [{"text": "retained original"}])
    event = {
        "event_id": "evt_fixture",
        "date": "2026-03-30",
        "timestamp": "2026-03-30T10:00:00Z",
        "source": "telegram",
        "kind": "message",
        "text": "Retained fixture",
        "raw_ref": {"path": str(raw), "locator": {"line": 1}},
    }
    normalized = old.normalized_event_path(event["date"])
    normalized.parent.mkdir(parents=True)
    normalized.write_text("\n" + json.dumps(event) + "\n")
    index = Path(build_index([old])["index"])
    packet = prepare_journal(old, day=event["date"], author="Fixture")
    revision = save_journal(old, packet_dir=packet, body="Retained. [fn:evt_fixture]", model="fake")
    hashes = {
        str(path.relative_to(old.root)): path.read_bytes()
        for path in old.root.rglob("*")
        if path.is_file()
    }
    new = tmp_path / "retained"
    shutil.move(old.root, new)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(old.root), "new": str(new)}]))
    return old, new, mapping, event, index, packet, revision, hashes


def test_readers_resolve_without_mutating_archive(tmp_path, monkeypatch):
    old, new, mapping, event, index, packet, revision, hashes = archive(tmp_path)
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    result = search(new / index.relative_to(old.root))[0]
    assert result["event"] == event
    assert result["normalized_path"] == str(old.normalized_event_path(event["date"]))
    assert result["resolved_normalized_path"] == str(new / "data/normalized/2026/2026-03-30.jsonl")
    assert result["citation_error"] is None
    assert result["raw_citation_error"] is None
    assert result["line"] == 2
    rendered = render_results([result], org=True)
    assert f"[[file:{new}/data/normalized/2026/2026-03-30.jsonl::2]" in rendered
    assert f"[[file:{new}/data/raw/fixture.jsonl::1]" in rendered
    assert _load_packet(packet)[1] == [event]
    record, body = _read_revision(revision)
    assert record["packet"] == str(packet)
    assert body == "Retained. [fn:evt_fixture]"
    for relative, data in hashes.items():
        assert (new / relative).read_bytes() == data


@pytest.mark.parametrize("damage", ["missing", "wrong", "line"])
def test_broken_citation_is_visible_not_a_valid_link(tmp_path, monkeypatch, damage):
    old, new, mapping, event, index, packet, revision, hashes = archive(tmp_path)
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    normalized = new / "data/normalized/2026/2026-03-30.jsonl"
    if damage == "missing":
        normalized.unlink()
    elif damage == "wrong":
        normalized.write_text("\n" + json.dumps({**event, "text": "different bytes"}) + "\n")
    else:
        normalized.write_text(json.dumps(event) + "\n")
    result = search(new / index.relative_to(old.root))[0]
    assert result["citation_error"]
    rendered = render_results([result], org=True)
    assert "Unresolved normalized citation" in rendered
    assert f"[[file:{normalized}" not in rendered
    raw = new / "data/raw/fixture.jsonl"
    raw.unlink()
    result = search(new / index.relative_to(old.root))[0]
    assert result["raw_citation_error"]
    assert "Unresolved raw citation" in render_results([result])


def test_mapping_is_explicit_authoritative_and_most_specific(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    mapping = tmp_path / "moves.json"
    mapping.write_text(
        json.dumps(
            [
                {"old": str(old), "new": str(tmp_path / "new")},
                {"old": str(old / "nested"), "new": str(tmp_path / "specific")},
            ]
        )
    )
    monkeypatch.delenv("RECALL_RELOCATION_MAP", raising=False)
    assert resolve_reference(old / "nested/a") == old / "nested/a"
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    assert resolve_reference(old / "nested/a") == tmp_path / "specific/a"
    assert resolve_reference(tmp_path / "older/a") == tmp_path / "older/a"
    mapping.write_text('[{"old":"relative","new":"/absolute"}]')
    with pytest.raises(ValueError, match="Invalid RECALL_RELOCATION_MAP"):
        resolve_reference(old)


def test_inspect_command_checks_relocated_packet_and_revision(tmp_path, monkeypatch):
    old, new, mapping, event, index, packet, revision, hashes = archive(tmp_path)
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    for option, path in [("--packet", packet), ("--revision", revision)]:
        result = CliRunner().invoke(app, ["journal", "inspect", option, str(path)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["events"] == 1
        assert data["packet_sha256"] == packet.name
        assert data["packet"] == str(new / packet.relative_to(old.root))
        assert not data["unresolved_citations"]
    (new / packet.relative_to(old.root) / "events.jsonl").write_text("changed\n")
    result = CliRunner().invoke(app, ["journal", "inspect", "--packet", str(packet)])
    assert result.exit_code != 0
    assert "Journal packet was changed" in result.output


def test_relative_raw_reference_uses_recorded_root_and_specific_mapping(tmp_path, monkeypatch):
    old = tmp_path / "old"
    mapping = tmp_path / "moves.json"
    destination = tmp_path / "retained-raw.json"
    destination.write_text("retained")
    mapping.write_text(
        json.dumps(
            [
                {"old": str(old), "new": str(tmp_path / "new")},
                {"old": str(old / "data/raw/fixture.json"), "new": str(destination)},
            ]
        )
    )
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    path, error = raw_reference(
        {"raw_ref": {"path": "data/raw/fixture.json"}},
        old / "data/normalized/2026/2026-03-30.jsonl",
    )
    assert path == destination and error is None


def test_inspect_reports_unresolved_without_writing(tmp_path, monkeypatch):
    old, new, mapping, event, index, packet, revision, hashes = archive(tmp_path)
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    (new / "data/raw/fixture.jsonl").unlink()
    result = CliRunner().invoke(app, ["journal", "inspect", "--packet", str(packet)])
    assert result.exit_code == 1
    assert json.loads(result.output)["unresolved_citations"][0]["raw_citation_error"]
    assert (new / packet.relative_to(old.root) / "packet.json").read_bytes() == hashes[
        str(packet.relative_to(old.root) / "packet.json")
    ]
