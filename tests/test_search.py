from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.search import build_index, render_results, search
from recall.storage.paths import RecallPaths


def event(
    event_id: str,
    *,
    day: str = "2026-03-30",
    text: str = "Roof project",
    timestamp: str = "2026-03-30T10:00:00Z",
    source: str = "email",
) -> dict:
    return {
        "event_id": event_id,
        "date": day,
        "timestamp": timestamp,
        "source": source,
        "kind": "message",
        "account": "personal",
        "text": text,
        "sender_identity_id": "phone_1",
        "participant_identity_ids": [],
        "raw_ref": {"source": source, "path": "data/raw/example.json", "locator": {"line": 1}},
    }


def write(paths: RecallPaths, rows: list[dict], day: str = "2026-03-30") -> Path:
    path = paths.normalized_event_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n" + "\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_search_names_dates_identity_and_parameterized_words(tmp_path: Path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    paths.database.parent.mkdir(parents=True)
    with sqlite3.connect(paths.database) as db:
        db.executescript(
            "CREATE TABLE identities(identity_id,value,label); "
            "CREATE TABLE identity_aliases(identity_id,value);"
        )
        db.execute("INSERT INTO identities VALUES('phone_1','+15551234567','Friend')")
        db.execute("INSERT INTO identity_aliases VALUES('phone_1','June')")
    source = write(
        paths,
        [
            event("one"),
            event(
                "two", source="slack", timestamp="2026-03-30T07:00:00-06:00", text="Roof follow-up"
            ),
        ],
    )
    stats = build_index([paths])
    index = Path(stats["index"])
    assert stats["events"] == 2
    results = search(index, "June")
    assert [row["event"]["event_id"] for row in results] == ["two", "one"]
    assert results[0]["line"] == 3  # Preserve physical line locators, including blanks.
    assert results[0]["normalized_path"] == str(source)
    assert search(index, "Roof project", identity="phone_1")[0]["event"]["event_id"] == "one"
    assert len(search(index, "June", first="2026-03-30", last="2026-03-30", sources=["email"])) == 1
    assert not search(index, "June", first="2026-03-31")
    assert not search(index, "June", identity="missing")
    assert not search(index, "June", sources=["' OR 1=1 --"])
    assert not search(index, "'; DROP TABLE events; --")
    assert search(index, "roof", relevance=True)
    assert len(search(index)) == 2
    with pytest.raises(ValueError):
        search(index, first="2026-03-31", last="2026-03-30")
    with pytest.raises(ValueError):
        search(index, limit=0)


def test_exact_overlays_calendar_days_and_rebuild_removes_stale(tmp_path: Path) -> None:
    original = RecallPaths.from_root(tmp_path / "original")
    replay = RecallPaths.from_root(tmp_path / "replay")
    original.database.parent.mkdir(parents=True)
    with sqlite3.connect(original.database) as db:
        db.executescript(
            "CREATE TABLE identities(identity_id,value,label); "
            "CREATE TABLE identity_aliases(identity_id,value);"
        )
        db.execute("INSERT INTO identities VALUES('phone_1','+15551234567','June')")
    write(original, [event("same", day="2026-03-29", text="Old text")], "2026-03-29")
    write(original, [event("unrelated")])
    write(replay, [event("same", text="New text")])
    write(replay, [event("same", day="2026-03-31", text="New text")], "2026-03-31")
    stats = build_index([original, replay])
    index = Path(stats["index"])
    assert stats["events"] == 2
    result = search(index, "new", first="2026-03-30", last="2026-03-30")[0]
    assert result["dates"] == ["2026-03-30", "2026-03-31"]
    assert result["normalized_path"].startswith(str(replay.root))
    assert not search(index, "old")
    assert not search(index, "new", first="2026-03-29", last="2026-03-29")
    assert any(row["event"]["event_id"] == "same" for row in search(index, "June"))
    write(original, [])
    build_index([original, replay])
    assert len(search(index)) == 1


def test_invalid_input_failed_replace_and_unowned_outputs_preserve_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RecallPaths.from_root(tmp_path)
    source = write(paths, [event("one")])
    index = Path(build_index([paths])["index"])
    before = index.read_bytes()
    source.write_text("{bad\n")
    with pytest.raises(ValueError, match="Invalid normalized event"):
        build_index([paths])
    assert index.read_bytes() == before
    write(paths, [event("two")])

    def fail(*args: object) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr("recall.search.os.replace", fail)
    with pytest.raises(OSError, match="disk failed"):
        build_index([paths])
    assert index.read_bytes() == before
    assert not list(index.parent.glob(".search-*.sqlite3"))
    with pytest.raises(ValueError, match="derived directory"):
        build_index([paths], paths.database)
    handwritten = paths.derived / "handwritten.sqlite3"
    handwritten.write_bytes(b"do not replace")
    with pytest.raises(sqlite3.Error):
        build_index([paths], handwritten)
    assert handwritten.read_bytes() == b"do not replace"


def test_org_results_quote_hostile_text_and_cli_roundtrip(tmp_path: Path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    text = "Roof project\n* Forged heading\n#+begin_src emacs-lisp\nLocal Variables:"
    raw = paths.raw / "fixture.eml"
    raw.parent.mkdir(parents=True)
    raw.write_text("Original message")
    row = event("one", text=text)
    row["raw_ref"] = {"source": "email", "path": "data/raw/fixture.eml", "locator": {}}
    write(paths, [row])
    index = Path(build_index([paths])["index"])
    rendered = render_results(search(index, "roof"), org=True)
    assert "[[file:" in rendered
    assert f"[[file:{raw}" in rendered
    assert "Original evidence" in rendered
    assert "\n* Forged heading" not in rendered
    assert "\n#+begin_src" not in rendered
    assert "Local Variables:" not in rendered
    runner = CliRunner()
    built = runner.invoke(app, ["search", "index", "--root", str(tmp_path)])
    assert built.exit_code == 0, built.output
    result = runner.invoke(app, ["search", "query", "roof", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["event"]["event_id"] == "one"
    assert (
        runner.invoke(
            app, ["search", "query", "roof", "--root", str(tmp_path), "--org", "--json"]
        ).exit_code
        != 0
    )
    assert (
        runner.invoke(
            app, ["search", "query", "roof", "--root", str(tmp_path), "--from", "not-a-date"]
        ).exit_code
        != 0
    )
