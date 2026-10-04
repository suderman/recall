from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.entities.observations import LABELS_PATH
from recall.search import build_index
from recall.storage.paths import RecallPaths


def fixture(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    path = paths.normalized_event_path("2026-04-02")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "event_id": "one",
                "date": "2026-04-02",
                "timestamp": "2026-04-02T00:00:00Z",
                "source": "slack",
                "kind": "message",
                "text": "fixture",
            }
        )
        + "\n"
    )
    build_index([paths])
    return paths, path


def status(paths):
    result = CliRunner().invoke(app, ["search", "status", "--root", str(paths.root), "--json"])
    return result, json.loads(result.stdout)


def fingerprint(path):
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def test_status_is_read_only_and_does_not_claim_capture_completeness(tmp_path):
    paths, path = fixture(tmp_path)
    index = paths.derived / "search.sqlite3"
    before = {p: fingerprint(p) for p in (path, index)}
    result, report = status(paths)
    assert result.exit_code == 0, result.output
    assert report["status"] == "unchanged" and report["events"] == 1
    assert report["sources"] == [
        {"source": "slack", "first_date": "2026-04-02", "last_date": "2026-04-02", "events": 1}
    ]
    assert report["inputs"][0]["status"] == "unchanged"
    assert "capture" in report["note"] and "completeness" in report["note"]
    assert status(paths)[1] == report
    assert {p: fingerprint(p) for p in before} == before


@pytest.mark.parametrize("change", ["same-size", "new-file", "deleted", "directory"])
def test_status_detects_input_changes(tmp_path, change):
    paths, path = fixture(tmp_path)
    if change == "same-size":
        path.write_text(path.read_text().replace("fixture", "changed"))
    elif change == "new-file":
        paths.normalized_event_path("2026-04-03").write_text("\n")
    else:
        path.unlink()
        if change == "directory":
            path.mkdir()
    result, report = status(paths)
    assert result.exit_code == 1
    assert report["status"] == ("unverifiable" if change == "directory" else "stale")
    if change == "new-file":
        assert report["added_inputs"] == [str(paths.normalized_event_path("2026-04-03"))]
    else:
        assert (
            report["inputs"][0]["status"]
            == {"same-size": "changed", "deleted": "missing", "directory": "unreadable"}[change]
        )


def test_status_keeps_overlay_order_and_empty_root(tmp_path):
    paths, _ = fixture(tmp_path / "base")
    overlay, _ = fixture(tmp_path / "overlay")
    empty = RecallPaths.from_root(tmp_path / "empty")
    empty.normalized.mkdir(parents=True)
    build_index([paths, empty, overlay])
    result, report = status(paths)
    assert result.exit_code == 0
    assert [r["recorded_root"] for r in report["roots"]] == [
        str(p.root) for p in (paths, empty, overlay)
    ]
    assert report["events"] == 1 and len(report["inputs"]) == 2
    shutil.rmtree(empty.root)
    result, report = status(paths)
    assert result.exit_code == 1 and report["roots"][1]["status"] == "missing"


def test_status_uses_authoritative_relocation(tmp_path, monkeypatch):
    paths, _ = fixture(tmp_path / "old")
    moved = RecallPaths.from_root(tmp_path / "moved")
    shutil.copytree(paths.root, moved.root)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(paths.root), "new": str(moved.root)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    # A changed old directory must not mask the accepted mapped input.
    paths.normalized_event_path("2026-04-02").write_text("stale duplicate")
    result, report = status(moved)
    assert result.exit_code == 0, result.output
    assert report["inputs"][0]["resolved_path"] == str(moved.normalized_event_path("2026-04-02"))
    assert report["roots"][0]["resolved_root"] == str(moved.root)


@pytest.mark.parametrize(
    "change", ["snapshot-add", "snapshot-change", "snapshot-delete", "database-label"]
)
def test_status_detects_identity_observation_changes_without_schema_migration(tmp_path, change):
    paths, _ = fixture(tmp_path)
    snapshot = paths.state / LABELS_PATH
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('{"identity_id":"id","labels":["Before"]}\n')
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.database) as db:
        db.executescript(
            "CREATE TABLE identities(identity_id,value,label); "
            "CREATE TABLE identity_aliases(identity_id,value); "
            "CREATE TABLE resolutions(identity_id);"
        )
        db.execute("INSERT INTO identities VALUES('id','value','Before')")
    if change == "snapshot-add":
        snapshot.unlink()
    build_index([paths])
    if change in {"snapshot-add", "snapshot-change"}:
        snapshot.write_text('{"identity_id":"id","labels":["After"]}\n')
    elif change == "snapshot-delete":
        snapshot.unlink()
    else:
        with sqlite3.connect(paths.database) as db:
            db.execute("UPDATE identities SET label='After'")
    before = fingerprint(paths.database)
    result, report = status(paths)
    assert result.exit_code == 1 and report["status"] == "stale"
    assert report["roots"][0]["identity_labels"] == (
        "unchanged" if change == "snapshot-delete" else "changed"
    )
    assert fingerprint(paths.database) == before
    with sqlite3.connect(paths.database) as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(resolutions)")] == ["identity_id"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing-index",
        "not-sqlite",
        "unowned",
        "bad-json",
        "relative-path",
        "bad-checksum",
        "missing-labels",
    ],
)
def test_status_reports_invalid_index_without_secret_details_or_initialization(tmp_path, damage):
    paths, _ = fixture(tmp_path)
    index = paths.derived / "search.sqlite3"
    secret = "FAKE_SECRET_DO_NOT_REPORT"
    if damage == "missing-index":
        index.unlink()
    elif damage == "not-sqlite":
        index.write_text(secret)
    else:
        with sqlite3.connect(index) as db:
            if damage == "unowned":
                db.execute("UPDATE metadata SET value=? WHERE key='format'", (secret,))
            else:
                manifest = json.loads(
                    db.execute("SELECT value FROM metadata WHERE key='inputs'").fetchone()[0]
                )
                if damage == "relative-path":
                    manifest["inputs"] = {"relative.jsonl": "a" * 64}
                elif damage == "bad-checksum":
                    manifest["inputs"] = {str(paths.root / "fixture"): secret}
                elif damage == "missing-labels":
                    del manifest["identity_labels"]
                value = secret if damage == "bad-json" else json.dumps(manifest)
                db.execute("UPDATE metadata SET value=? WHERE key='inputs'", (value,))
    before = fingerprint(index) if index.exists() else None
    result, report = status(paths)
    assert result.exit_code == 1 and report["status"] == "unverifiable"
    assert secret not in result.output
    assert (fingerprint(index) if index.exists() else None) == before


def test_status_missing_store_and_invalid_map_fail_without_creating_files(tmp_path, monkeypatch):
    absent = RecallPaths.from_root(tmp_path / "absent")
    assert status(absent)[0].exit_code == 1 and not absent.root.exists()
    paths, _ = fixture(tmp_path)
    mapping = tmp_path / "moves.json"
    mapping.write_text("FAKE_SECRET")
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    result, report = status(paths)
    assert result.exit_code == 1 and report["status"] == "unverifiable"
    assert "FAKE_SECRET" not in result.output


def test_file_only_relocation_never_uses_recreated_old_inputs(tmp_path, monkeypatch):
    paths, normalized = fixture(tmp_path)
    snapshot = paths.state / LABELS_PATH
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('{"identity_id":"id","labels":["Before"]}\n')
    build_index([paths])
    retained = tmp_path / "retained"
    retained.mkdir()
    mappings = []
    for path in (normalized, snapshot):
        new = retained / path.name
        shutil.copy2(path, new)
        mappings.append({"old": str(path), "new": str(new)})
        path.write_text("FAKE_SECRET_RECREATED_PATH")
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps(mappings))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    result, report = status(paths)
    assert result.exit_code == 0, result.output
    assert report["roots"][0]["identity_labels"] == "unchanged"
    assert report["added_inputs"] == []


def test_status_unreadable_normalized_input_and_text_output(tmp_path, monkeypatch):
    paths, path = fixture(tmp_path)
    original = Path.open

    def denied(p, *args, **kwargs):
        if p == path:
            raise PermissionError("FAKE_SECRET")
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result, report = status(paths)
    assert result.exit_code == 1 and report["inputs"][0]["status"] == "unreadable"
    assert "FAKE_SECRET" not in result.output
    result = CliRunner().invoke(app, ["search", "status", "--root", str(paths.root)])
    assert result.exit_code == 1 and "unverifiable" in result.stdout
