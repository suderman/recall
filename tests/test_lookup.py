from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from recall import lookup
from recall.cli.main import app
from recall.search import build_index, search
from recall.storage.paths import RecallPaths


def workspace(tmp_path: Path, identities: int = 2) -> tuple[RecallPaths, Path]:
    paths = RecallPaths.from_root(tmp_path)
    paths.database.parent.mkdir(parents=True)
    with sqlite3.connect(paths.database) as db:
        db.executescript(
            "CREATE TABLE identities(identity_id,value,label); "
            "CREATE TABLE identity_aliases(identity_id,value);"
        )
        db.executemany(
            "INSERT INTO identities VALUES(?,?,?)",
            [(f"id_{n}", f"alex{n}@example.test", "Alex") for n in range(identities)],
        )
    rows = [
        {
            "event_id": f"event_{n}",
            "date": "2026-03-30",
            "timestamp": "2026-03-30T10:00:00Z",
            "source": "email",
            "kind": "email",
            "text": f"Roof project details {n}",
            "sender_identity_id": f"id_{n}",
            "participant_identity_ids": [],
            "raw_ref": {},
        }
        for n in range(identities)
    ]
    # A name in the body is not an observed identity label.
    rows.append(
        {
            **rows[0],
            "event_id": "mentioned",
            "sender_identity_id": "unknown",
            "text": "Ask Alex and Taylor",
        }
    )
    # Lexical timestamp ordering would place this first, but its instant is later.
    rows.append({**rows[0], "event_id": "latest", "timestamp": "2026-03-30T07:00:00-06:00"})
    path = paths.normalized_event_path("2026-03-30")
    path.parent.mkdir(parents=True)
    path.write_text("\n" + "\n".join(json.dumps(row) for row in rows) + "\n")
    return paths, Path(build_index([paths])["index"])


def test_person_candidates_keep_ambiguity_and_citations_read_only(tmp_path: Path) -> None:
    paths, index = workspace(tmp_path)
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in (index, paths.database)}
    packet = lookup.person(index, "Alex", limit=1)
    assert packet["matched_identities"] == 2
    assert packet["ambiguous"]
    assert {c["identity_id"] for c in packet["candidates"]} == {"id_0", "id_1"}
    candidate = packet["candidates"][0]
    assert candidate["first_captured"]["event"]["event_id"] == "event_0"
    assert candidate["last_captured"]["event"]["event_id"] == "latest"
    assert len(candidate["recent"]) == 1
    for c in packet["candidates"]:
        for row in [c["first_captured"], *c["recent"]]:
            line = Path(row["normalized_path"]).read_text().splitlines()[row["line"] - 1]
            assert json.loads(line)["event_id"] == row["event"]["event_id"]
    assert not lookup.person(index, "Alexandra")["candidates"]
    mentions = lookup.person(index, "Taylor")
    assert not mentions["candidates"]
    assert mentions["unlinked_name_mentions"][0]["event"]["event_id"] == "mentioned"
    assert "Unlinked name mentions" in lookup.render_packet(mentions)
    assert lookup.person(index, identity="unknown")["candidates"][0]["observed_labels"] == []
    assert lookup.person(index, identity="missing")["matched_identities"] == 0
    assert len(lookup.person(index, identity="id_1")["candidates"]) == 1
    assert search(index, "Roof", oldest=True)[0]["event"]["event_id"] == "event_0"
    assert before == {p: hashlib.sha256(p.read_bytes()).digest() for p in before}


def test_scopes_and_broad_candidates_are_explicit(tmp_path: Path) -> None:
    _, index = workspace(tmp_path, identities=25)
    packet = lookup.person(index, "Alex")
    assert packet["matched_identities"] == 25
    assert packet["candidates_truncated"]
    assert len(packet["candidates"]) == 20
    selected = lookup.person(index, identity="id_24", sources=["slack"])
    assert selected["candidates"][0]["last_captured"] is None
    assert lookup.person(index, "Alex", first="2026-03-31")["candidates"][0]["recent"] == []
    with pytest.raises(ValueError):
        lookup.person(index, "No label matches", limit=0)
    with pytest.raises(ValueError):
        lookup.person(index, "No label matches", first="2026-04-01", last="2026-03-30")
    with pytest.raises(ValueError, match="Provide a name"):
        lookup.person(index)


def test_project_has_literal_history_not_a_status_guess(tmp_path: Path) -> None:
    _, index = workspace(tmp_path)
    packet = lookup.project(index, "Roof project", limit=1)
    assert packet["first_captured"]["event"]["event_id"] == "event_0"
    assert packet["last_captured"]["event"]["event_id"] == "latest"
    assert packet["scope"]["recent_limit"] == 1
    assert "project completion" in packet["note"]
    assert lookup.project(index, "Roof missing")["recent"] == []
    assert lookup.project(index, "Roof", sources=["slack"])["recent"] == []
    with pytest.raises(ValueError, match="Provide project"):
        lookup.project(index, "!!!")


def test_org_output_quotes_sources_and_cli_returns_packets(tmp_path: Path) -> None:
    paths, index = workspace(tmp_path)
    with sqlite3.connect(paths.database) as db:
        db.execute(
            "UPDATE identities SET label=?", ("Alex\n* Forged\n#+begin_src\nLocal Variables:",)
        )
    build_index([paths])
    rendered = lookup.render_packet(lookup.person(index, "Alex"), org=True)
    assert rendered.count("#+TITLE:") == 1
    assert "[[file:" in rendered
    assert "\n* Forged" not in rendered
    assert "\n#+begin_src" not in rendered
    assert "Local Variables:" not in rendered
    runner = CliRunner()
    for command, query in (("person", "Alex"), ("project", "Roof")):
        result = runner.invoke(app, ["search", command, query, "--root", str(tmp_path), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["kind"] == command
        bad = runner.invoke(
            app, ["search", command, query, "--root", str(tmp_path), "--json", "--org"]
        )
        assert bad.exit_code != 0
    missing = runner.invoke(app, ["search", "person", "Alex", "--root", str(tmp_path / "missing")])
    assert missing.exit_code != 0
    assert not (tmp_path / "missing").exists()


def test_changed_index_refuses_mixed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, index = workspace(tmp_path)
    original = lookup.search

    def replace(index_arg: Path, text: str = "", **kwargs: Any) -> list:
        build_index([paths])
        return original(index_arg, text, **kwargs)

    monkeypatch.setattr(lookup, "search", replace)
    with pytest.raises(ValueError, match="changed during lookup"):
        lookup.project(index, "Roof")


def test_read_access_time_is_not_an_index_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, index = workspace(tmp_path)
    original = lookup.search

    def read(index_arg: Path, text: str = "", **kwargs: Any) -> list:
        stat = index.stat()
        os.utime(index, ns=(stat.st_atime_ns + 1_000_000_000, stat.st_mtime_ns))
        return original(index_arg, text, **kwargs)

    monkeypatch.setattr(lookup, "search", read)
    assert lookup.project(index, "Roof")["recent"]


def test_corrupt_metadata_requests_rebuild(tmp_path: Path) -> None:
    _, index = workspace(tmp_path)
    with sqlite3.connect(index) as db:
        db.execute("UPDATE metadata SET value='invalid' WHERE key='inputs'")
    with pytest.raises(ValueError, match="rebuild the index"):
        lookup.person(index, "Alex")
