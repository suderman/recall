from __future__ import annotations

from typer.testing import CliRunner

from recall.cli.main import app


runner = CliRunner()


def test_init_creates_foundation(tmp_path) -> None:
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "data" / "state" / "recall.sqlite3").exists()
    assert (tmp_path / "data" / "normalized").is_dir()


def test_paths_reports_workspace_locations(tmp_path) -> None:
    result = runner.invoke(app, ["paths", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert f"root={tmp_path.resolve()}" in result.stdout
    assert f"database={tmp_path.resolve() / 'data' / 'state' / 'recall.sqlite3'}" in result.stdout
