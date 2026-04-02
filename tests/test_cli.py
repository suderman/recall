from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import recall.cli.artifacts as artifacts_cli
from recall.cli.main import app
from recall.normalize.artifacts import NormalizedArtifact, RemoteLocator
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_artifact_metadata, write_normalized_events
from recall.storage.paths import RecallPaths

runner = CliRunner()
SLACK_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "slack_capture"
TELEGRAM_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telegram"


def _copy_slack_fixture_capture(tmp_path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("slack", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in ("metadata.json", "conversations.json", "messages.jsonl"):
        shutil.copy(SLACK_FIXTURE_DIR / name, target_dir / name)

    return paths


def _copy_telegram_fixture_capture(tmp_path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(TELEGRAM_FIXTURE_DIR / "updates.jsonl", target_dir / "updates.jsonl")

    return paths


def test_init_creates_foundation(tmp_path) -> None:
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "data" / "state" / "recall.sqlite3").exists()
    assert (tmp_path / "data" / "normalized").is_dir()


def test_paths_reports_workspace_locations(tmp_path) -> None:
    result = runner.invoke(app, ["paths", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert f"root={tmp_path.resolve()}" in result.stdout
    assert f"artifacts={tmp_path.resolve() / 'data' / 'artifacts'}" in result.stdout
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
                raw_ref=RawReference(
                    source="slack",
                    path="data/raw/slack/2026-03-31/messages.jsonl",
                    locator={"channel": "C123", "ts": "1774976467.000100"},
                ),
            )
        ],
    )

    result = runner.invoke(app, ["events", "show", "--root", str(tmp_path), "--date", "2026-03-31"])

    assert result.exit_code == 0
    assert "date=2026-03-31" in result.stdout
    assert "count=1" in result.stdout
    assert "hello world" in result.stdout
    assert '"path": "data/raw/slack/2026-03-31/messages.jsonl"' in result.stdout


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


def test_artifacts_show_reports_daily_artifacts(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[
            NormalizedArtifact(
                artifact_id="artifact_1",
                source="slack",
                kind="file",
                filename="diagram.png",
                event_ids=["evt_1"],
                remote_locators=[
                    RemoteLocator(kind="url_private", value="https://files.example/test")
                ],
                download_status="not_requested",
                raw_ref=RawReference(
                    source="slack",
                    path="data/raw/slack/2026-03-31/messages.jsonl",
                    locator={"channel": "C123", "ts": "1774976467.000100", "file_id": "F123"},
                ),
            )
        ],
    )

    result = runner.invoke(
        app,
        ["artifacts", "show", "--root", str(tmp_path), "--date", "2026-03-31", "--source", "slack"],
    )

    assert result.exit_code == 0
    assert "date=2026-03-31" in result.stdout
    assert "source=slack" in result.stdout
    assert "diagram.png" in result.stdout
    assert "artifact_id=artifact_1" in result.stdout
    assert "local_path=- checksums=-" in result.stdout


def test_artifacts_show_json_outputs_artifact_list(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[NormalizedArtifact(artifact_id="artifact_1", source="slack", kind="file")],
    )

    result = runner.invoke(
        app,
        [
            "artifacts",
            "show",
            "--root",
            str(tmp_path),
            "--date",
            "2026-03-31",
            "--source",
            "slack",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"artifact_id": "artifact_1"' in result.stdout


def test_artifacts_show_reports_downloaded_local_mirror(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[
            NormalizedArtifact(
                artifact_id="artifact_1",
                source="slack",
                kind="file",
                filename="diagram.png",
                local_path="data/artifacts/blobs/slack/2026/2026-03-31/artifact_1--diagram.png",
                checksums={"sha256": "abc123"},
                download_status="downloaded",
            )
        ],
    )

    result = runner.invoke(
        app,
        ["artifacts", "show", "--root", str(tmp_path), "--date", "2026-03-31", "--source", "slack"],
    )

    assert result.exit_code == 0
    assert "download_status=downloaded" in result.stdout
    assert (
        "local_path=data/artifacts/blobs/slack/2026/2026-03-31/artifact_1--diagram.png"
        in result.stdout
    )
    assert "checksums=sha256:abc123" in result.stdout


def test_artifacts_show_reports_download_failure_detail(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[
            NormalizedArtifact(
                artifact_id="artifact_1",
                source="slack",
                kind="file",
                download_status="failed",
                last_error="404 Not Found",
            )
        ],
    )

    result = runner.invoke(
        app,
        ["artifacts", "show", "--root", str(tmp_path), "--date", "2026-03-31", "--source", "slack"],
    )

    assert result.exit_code == 0
    assert "download_status=failed" in result.stdout
    assert "last_error=404 Not Found" in result.stdout


def test_artifacts_download_reports_missing_normalize_step(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")

    result = runner.invoke(
        app,
        ["artifacts", "download", "slack", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code != 0
    assert "No slack artifact metadata found for 2026-03-31" in result.output


def test_artifacts_download_reports_metadata_only_guidance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[NormalizedArtifact(artifact_id="artifact_1", source="slack", kind="file")],
    )

    result = runner.invoke(
        app,
        ["artifacts", "download", "slack", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "download_mode=metadata-only" in result.stdout
    assert "--policy download-source-native" in result.stdout


def test_artifacts_download_reports_files_read_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="slack",
        date="2026-03-31",
        artifacts=[
            NormalizedArtifact(
                artifact_id="artifact_1",
                source="slack",
                kind="file",
                download_status="failed",
                last_error="Redirect response '302 Found' for url 'https://files.slack.com/foo'",
            )
        ],
    )

    def fake_download(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            artifact_path=paths.artifact_metadata_path("slack", "2026-03-31"),
            artifacts_seen=1,
            would_download=1,
            downloaded=0,
            skipped_policy=0,
            skipped_existing=0,
            failed=1,
        )

    monkeypatch.setattr(artifacts_cli, "download_slack_artifacts", fake_download)

    result = runner.invoke(
        app,
        [
            "artifacts",
            "download",
            "slack",
            "--root",
            str(tmp_path),
            "--date",
            "2026-03-31",
            "--policy",
            "download-source-native",
        ],
    )

    assert result.exit_code == 0
    assert "files:read" in result.stdout


def test_artifacts_download_bluebubbles_reports_metadata_only_guidance(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
    config_dir = tmp_path / "config" / "sources"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bluebubbles.toml").write_text(
        (
            'account = "personal"\n'
            'server_url = "http://10.1.0.9:1234"\n'
            'password_env_var = "BLUEBUBBLES_PASSWORD"\n'
            'artifact_download_policy = "metadata-only"\n'
        ),
        encoding="utf-8",
    )
    paths = RecallPaths.from_root(tmp_path)
    write_artifact_metadata(
        paths,
        source="bluebubbles",
        date="2026-03-31",
        artifacts=[
            NormalizedArtifact(artifact_id="artifact_1", source="bluebubbles", kind="attachment")
        ],
    )

    result = runner.invoke(
        app,
        ["artifacts", "download", "bluebubbles", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "source=bluebubbles" in result.stdout
    assert "download_mode=metadata-only" in result.stdout
    assert "--policy download-source-native" in result.stdout


def test_normalize_slack_reports_next_steps(tmp_path) -> None:
    paths = _copy_slack_fixture_capture(tmp_path)
    assert paths.raw_capture_dir("slack", "2026-03-31").exists()

    result = runner.invoke(
        app,
        ["normalize", "slack", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "recall artifacts show --date 2026-03-31" in result.stdout


def test_capture_bluebubbles_serve_reports_webhook_url_hint(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config" / "sources"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bluebubbles.toml").write_text(
        (
            'account = "personal"\n'
            'webhook_bind_host = "0.0.0.0"\n'
            "webhook_port = 8042\n"
            'webhook_token = "secret"\n'
        ),
        encoding="utf-8",
    )

    import recall.connectors.bluebubbles.cli as bluebubbles_cli

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("stop after startup output")

    monkeypatch.setattr(bluebubbles_cli.uvicorn, "run", fake_run)

    result = runner.invoke(app, ["capture", "bluebubbles", "serve", "--root", str(tmp_path)])

    assert result.exit_code != 0
    assert (
        "webhook_url_hint=use http://<recall-host-lan-ip>:8042/bluebubbles/webhook?token=secret"
        in result.stdout
    )


def test_import_bluebubbles_export_reports_next_steps(tmp_path) -> None:
    export_dir = tmp_path / "bluebubbles-export"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "manifest.json").write_text(
        '{"export_id":"bb_hist_20260331","source":"bluebubbles","schema_version":1}',
        encoding="utf-8",
    )
    (export_dir / "messages.jsonl").write_text(
        (
            '{"guid":"msg-1","dateCreated":1774978267000,'
            '"text":"Historical hello","chatGuid":"iMessage;+15551234567",'
            '"handle":"+15551234567","participants":["+15551234567"]}\n'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["import", "bluebubbles-export", str(export_dir), "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "mode=import" in result.stdout
    assert "messages_imported=1" in result.stdout
    assert "recall normalize bluebubbles --date YYYY-MM-DD" in result.stdout


def test_entities_sync_slack_persists_identity_rows(tmp_path) -> None:
    paths = _copy_slack_fixture_capture(tmp_path)

    result = runner.invoke(
        app,
        ["entities", "sync", "slack", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "identities=3" in result.stdout
    assert "identity_aliases=2" in result.stdout

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]

    assert identity_count == 3


def test_normalize_telegram_reports_next_steps(tmp_path) -> None:
    _copy_telegram_fixture_capture(tmp_path)

    result = runner.invoke(
        app,
        ["normalize", "telegram", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "recall entities sync telegram --date 2026-03-31" in result.stdout


def test_capture_telegram_append_reports_cursor_and_next_step(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "append",
            str(TELEGRAM_FIXTURE_DIR / "update.json"),
            "--root",
            str(tmp_path),
            "--update-type",
            "updateNewMessage",
            "--update-id",
            "12345",
            "--received-at",
            "2026-03-31T18:00:00Z",
        ],
    )

    assert result.exit_code == 0
    assert "mode=append" in result.stdout
    assert "cursor_key=last_update_id" in result.stdout
    assert "cursor_value=12345" in result.stdout
    assert "recall normalize telegram --date 2026-03-31" in result.stdout


def test_capture_telegram_once_reports_single_update(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "once",
            str(TELEGRAM_FIXTURE_DIR / "update_stream.jsonl"),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "mode=once" in result.stdout
    assert "captured_updates=1" in result.stdout
    assert "last_update_id=12345" in result.stdout


def test_capture_telegram_run_respects_after_update_id(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "run",
            str(TELEGRAM_FIXTURE_DIR / "update_stream.jsonl"),
            "--root",
            str(tmp_path),
            "--after-update-id",
            "12345",
        ],
    )

    assert result.exit_code == 0
    assert "mode=run" in result.stdout
    assert "captured_updates=1" in result.stdout
    assert "last_update_id=12346" in result.stdout
    assert "cursor_value=12346" in result.stdout


def test_capture_telegram_tdlib_once_reports_tdlib_transport(tmp_path, monkeypatch) -> None:
    import recall.connectors.telegram.cli as telegram_cli

    def fake_capture(**kwargs):
        assert kwargs["tdlib_log_verbosity_level"] is None
        typer = __import__("typer")
        typer.echo("mode=tdlib-once")
        typer.echo("transport=tdlib")
        typer.echo("captured_updates=1")
        typer.echo("last_update_id=77")

    monkeypatch.setattr(telegram_cli, "_capture_updates_from_tdlib", fake_capture)

    result = runner.invoke(app, ["capture", "telegram", "tdlib-once", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "mode=tdlib-once" in result.stdout
    assert "transport=tdlib" in result.stdout
    assert "captured_updates=1" in result.stdout


def test_capture_telegram_tdlib_run_reports_tdlib_transport(tmp_path, monkeypatch) -> None:
    import recall.connectors.telegram.cli as telegram_cli

    def fake_capture(**kwargs):
        assert kwargs["tdlib_log_verbosity_level"] == 2
        typer = __import__("typer")
        typer.echo("mode=tdlib-run")
        typer.echo("transport=tdlib")
        typer.echo("captured_updates=2")

    monkeypatch.setattr(telegram_cli, "_capture_updates_from_tdlib", fake_capture)

    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "tdlib-run",
            "--root",
            str(tmp_path),
            "--tdlib-log-verbosity-level",
            "2",
            "--max-updates",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "mode=tdlib-run" in result.stdout
    assert "transport=tdlib" in result.stdout
    assert "captured_updates=2" in result.stdout


def test_capture_telegram_tdlib_daemon_uses_stored_cursor(tmp_path, monkeypatch) -> None:
    import recall.connectors.telegram.cli as telegram_cli
    from recall.storage.paths import RecallPaths
    from recall.storage.state import set_connector_cursor

    paths = RecallPaths.from_root(tmp_path)
    paths.ensure_directories()
    set_connector_cursor(
        paths,
        source="telegram",
        account="personal",
        cursor_key="last_update_id",
        cursor_value="12345",
        updated_at="2026-03-31T18:00:00Z",
    )

    class FakeClient:
        def close(self) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    from types import SimpleNamespace

    monkeypatch.setattr(
        telegram_cli,
        "build_tdlib_auth_settings",
        lambda *args, **kwargs: SimpleNamespace(library_path=None, log_verbosity_level=0),
    )
    monkeypatch.setattr(telegram_cli, "TdlibJsonTransport", lambda *args, **kwargs: FakeTransport())
    monkeypatch.setattr(
        telegram_cli,
        "TdlibTelegramClient",
        lambda *args, **kwargs: FakeClient(),
    )

    calls: list[int | None] = []

    def fake_capture_updates(*args, **kwargs):
        del args
        calls.append(kwargs["after_update_id"])
        from types import SimpleNamespace

        return SimpleNamespace(
            captured_updates=0, dates_written=[], last_update_id=None, cursor=None
        )

    monkeypatch.setattr(telegram_cli, "capture_telegram_updates", fake_capture_updates)
    monkeypatch.setattr(telegram_cli.time, "sleep", lambda seconds: None)

    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "tdlib-daemon",
            "--root",
            str(tmp_path),
            "--max-cycles",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert calls == [12345]
    assert "mode=tdlib-daemon" in result.stdout
    assert "start_after_update_id=12345" in result.stdout
    assert "cycle=1 captured_updates=0 last_update_id=-" in result.stdout


def test_capture_telegram_tdlib_daemon_advances_cursor_between_cycles(
    tmp_path, monkeypatch
) -> None:
    import recall.connectors.telegram.cli as telegram_cli

    class FakeClient:
        def close(self) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    from types import SimpleNamespace

    monkeypatch.setattr(
        telegram_cli,
        "build_tdlib_auth_settings",
        lambda *args, **kwargs: SimpleNamespace(library_path=None, log_verbosity_level=0),
    )
    monkeypatch.setattr(telegram_cli, "TdlibJsonTransport", lambda *args, **kwargs: FakeTransport())
    monkeypatch.setattr(
        telegram_cli,
        "TdlibTelegramClient",
        lambda *args, **kwargs: FakeClient(),
    )

    responses = [(2, 200), (0, None)]
    calls: list[int | None] = []

    def fake_capture_updates(*args, **kwargs):
        del args
        calls.append(kwargs["after_update_id"])
        captured_updates, last_update_id = responses.pop(0)
        from types import SimpleNamespace

        return SimpleNamespace(
            captured_updates=captured_updates,
            dates_written=["2026-03-31"] if captured_updates else [],
            last_update_id=last_update_id,
            cursor=None,
        )

    sleeps: list[float] = []
    monkeypatch.setattr(telegram_cli, "capture_telegram_updates", fake_capture_updates)
    monkeypatch.setattr(telegram_cli.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = runner.invoke(
        app,
        [
            "capture",
            "telegram",
            "tdlib-daemon",
            "--root",
            str(tmp_path),
            "--after-update-id",
            "100",
            "--max-cycles",
            "2",
            "--idle-sleep-seconds",
            "0.5",
        ],
    )

    assert result.exit_code == 0
    assert calls == [100, 200]
    assert sleeps == [0.5]
    assert "cycle=1 captured_updates=2 last_update_id=200" in result.stdout
    assert "cycle=2 captured_updates=0 last_update_id=-" in result.stdout


def test_entities_sync_telegram_persists_identity_rows(tmp_path) -> None:
    paths = _copy_telegram_fixture_capture(tmp_path)

    result = runner.invoke(
        app,
        ["entities", "sync", "telegram", "--root", str(tmp_path), "--date", "2026-03-31"],
    )

    assert result.exit_code == 0
    assert "identities=3" in result.stdout
    assert "identity_aliases=6" in result.stdout

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]

    assert identity_count == 3


def test_state_show_slack_reports_empty_state(tmp_path) -> None:
    result = runner.invoke(app, ["state", "show", "slack", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "source=slack" in result.stdout
    assert "cursor_state=empty" in result.stdout


def test_state_show_telegram_reports_empty_state(tmp_path) -> None:
    result = runner.invoke(app, ["state", "show", "telegram", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "source=telegram" in result.stdout
    assert "cursor_state=empty" in result.stdout
