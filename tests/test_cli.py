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


def _copy_slack_fixture_capture(tmp_path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("slack", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in ("metadata.json", "conversations.json", "messages.jsonl"):
        shutil.copy(SLACK_FIXTURE_DIR / name, target_dir / name)

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


def test_state_show_slack_reports_empty_state(tmp_path) -> None:
    result = runner.invoke(app, ["state", "show", "slack", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "source=slack" in result.stdout
    assert "cursor_state=empty" in result.stdout
