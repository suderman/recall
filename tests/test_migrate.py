from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.entities.query import list_resolutions
from recall.storage.db import SchemaMigrationRequired, migrate_database
from recall.storage.paths import RecallPaths


def _legacy_database(paths: RecallPaths) -> None:
    paths.state.mkdir(parents=True)
    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            """
            create table persons (person_id text primary key, display_name text not null,
              sort_name text, notes text not null default '', tags_json text not null
              default '[]', created_at text not null);
            create table identities (identity_id text primary key, person_id text,
              source text not null, kind text not null, value text not null, label text,
              is_primary boolean not null default 0, status text not null default 'active',
              valid_from text, valid_to text, created_at text not null);
            create table resolutions (resolution_id text primary key,
              identity_id text not null, person_id text not null, confidence text not null,
              method text not null, evidence_json text not null default '[]',
              created_at text not null);
            insert into persons values ('person_1', 'Alex', null, '', '[]', '2026-04-02');
            """
        )


def _columns(paths: RecallPaths) -> set[str]:
    with sqlite3.connect(paths.database) as connection:
        return {row[1] for row in connection.execute("pragma table_info(resolutions)")}


def test_reading_a_legacy_database_requires_explicit_migration(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    _legacy_database(paths)
    before = paths.database.read_bytes()

    with pytest.raises(SchemaMigrationRequired, match="recall migrate"):
        list_resolutions(paths)

    assert paths.database.read_bytes() == before
    assert "valid_from" not in _columns(paths)


def test_migrate_backs_up_then_adds_resolution_validity(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    _legacy_database(paths)
    before = paths.database.read_bytes()

    report = migrate_database(paths)

    assert report["added"] == ["resolutions.valid_from", "resolutions.valid_to"]
    backup = paths.root / report["backup"]
    assert backup.parent == paths.state / "backups"
    assert backup.stat().st_mode & 0o077 == 0
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select display_name from persons").fetchall() == [("Alex",)]
    assert {"valid_from", "valid_to"} <= _columns(paths)
    assert paths.database.read_bytes() != before
    assert list_resolutions(paths) == []
    assert migrate_database(paths) == {"database": report["database"], "added": [], "backup": None}


def test_migrate_cli_reports_and_is_idempotent(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    _legacy_database(paths)
    runner = CliRunner()

    first = runner.invoke(app, ["migrate", "--root", str(tmp_path)])
    second = runner.invoke(app, ["migrate", "--root", str(tmp_path)])

    assert first.exit_code == 0, first.output
    assert "Added resolutions.valid_from, resolutions.valid_to" in first.output
    assert second.exit_code == 0, second.output
    assert "already current" in second.output


def test_migrate_refuses_a_missing_database(tmp_path) -> None:
    with pytest.raises(ValueError, match="No Recall database"):
        migrate_database(RecallPaths.from_root(tmp_path))
