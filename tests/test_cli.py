from __future__ import annotations

from typer.testing import CliRunner

from recall.cli.main import app
from recall.normalize.events import NormalizedEvent
from recall.storage.jsonl import write_normalized_events
from recall.storage.paths import RecallPaths

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


def test_events_show_reports_daily_events(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_normalized_events(
        paths,
        "2026-03-31",
        [
            NormalizedEvent(
                event_id="evt_1",
                source="slack",
                timestamp="2026-03-31T17:31:07Z",
                date="2026-03-31",
                kind="message",
                conversation_label="#webteam",
                sender_identity_id="ident_slack_USELF",
                participant_identity_ids=["ident_slack_UPEER", "ident_slack_USELF"],
                text="hello world",
                raw_ref="data/raw/slack/2026-03-31/messages.jsonl:1",
            )
        ],
    )

    result = runner.invoke(app, ["events", "show", "--root", str(tmp_path), "--date", "2026-03-31"])

    assert result.exit_code == 0
    assert "date=2026-03-31" in result.stdout
    assert "count=1" in result.stdout
    assert "hello world" in result.stdout


def test_events_show_json_outputs_event_list(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_normalized_events(
        paths,
        "2026-03-31",
        [
            NormalizedEvent(
                event_id="evt_1",
                source="slack",
                timestamp="2026-03-31T17:31:07Z",
                date="2026-03-31",
                kind="message",
                text="hello json",
            )
        ],
    )

    result = runner.invoke(
        app,
        ["events", "show", "--root", str(tmp_path), "--date", "2026-03-31", "--json"],
    )

    assert result.exit_code == 0
    assert '"event_id": "evt_1"' in result.stdout
    assert '"text": "hello json"' in result.stdout
