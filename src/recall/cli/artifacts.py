from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.bluebubbles.artifacts import download_bluebubbles_artifacts
from recall.connectors.bluebubbles.config import bluebubbles_config_path, load_bluebubbles_config
from recall.connectors.slack.artifacts import download_slack_artifacts
from recall.connectors.slack.config import load_slack_config, slack_config_path
from recall.connectors.telegram.artifacts import download_telegram_artifacts
from recall.connectors.telegram.config import load_telegram_config
from recall.connectors.telegram.tdlib import (
    TdlibJsonTransport,
    TdlibTelegramClient,
    build_tdlib_auth_settings,
)
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def _missing_artifact_metadata_message(paths: RecallPaths, *, source: str, date: str) -> str:
    artifact_path = paths.artifact_metadata_path(source, date)
    return (
        f"No {source} artifact metadata found for {date} at {artifact_path}. "
        f"Run 'recall normalize {source} --date {date}' first."
    )


def _looks_like_slack_scope_error(last_error: str | None) -> bool:
    if not last_error:
        return False

    indicators = (
        "302 Found",
        "slack.com/?redir=",
        "403 Forbidden",
        "401 Unauthorized",
        "login",
    )
    return any(indicator in last_error for indicator in indicators)


def _looks_like_bluebubbles_auth_error(last_error: str | None) -> bool:
    if not last_error:
        return False

    indicators = ("401", "403", "Unauthorized", "Forbidden", "guid=")
    return any(indicator in last_error for indicator in indicators)


def _render_artifact(artifact: dict[str, Any]) -> list[str]:
    label = artifact.get("filename") or artifact.get("source_object_id") or "-"
    artifact_id = artifact.get("artifact_id") or "-"
    download_status = artifact.get("download_status") or "-"
    event_ids = ",".join(artifact.get("event_ids", [])) or "-"
    local_path = artifact.get("local_path") or "-"
    last_error = artifact.get("last_error") or "-"
    checksums = artifact.get("checksums") or {}
    checksum_text = ",".join(f"{name}:{value}" for name, value in sorted(checksums.items())) or "-"
    remote_locators = (
        ",".join(locator["kind"] for locator in artifact.get("remote_locators", [])) or "-"
    )
    raw_ref = artifact.get("raw_ref")
    raw_ref_text = json.dumps(raw_ref, ensure_ascii=True, sort_keys=True) if raw_ref else "-"

    lines = [
        f"- {artifact['source']}:{artifact['kind']} {label}",
        f"  artifact_id={artifact_id} download_status={download_status}",
        f"  event_ids={event_ids} remote_locators={remote_locators}",
        f"  local_path={local_path} checksums={checksum_text}",
        f"  last_error={last_error}",
        f"  raw_ref={raw_ref_text}",
    ]
    return lines


def show_artifacts(
    date: str = typer.Option(..., "--date", help="Date to inspect in YYYY-MM-DD format."),
    source: str = typer.Option("slack", "--source", help="Artifact source to inspect."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum artifacts to print."),
    as_json: bool = typer.Option(False, "--json", help="Print matching artifacts as JSON."),
) -> None:
    """Show normalized artifact metadata for one day."""

    paths = _paths_for(root)
    artifact_path = paths.artifact_metadata_path(source, date)
    if not artifact_path.exists():
        raise typer.BadParameter(
            _missing_artifact_metadata_message(paths, source=source, date=date)
        )

    artifacts = read_jsonl(artifact_path)
    if limit is not None:
        artifacts = artifacts[:limit]

    if as_json:
        typer.echo(json.dumps(artifacts, indent=2, ensure_ascii=True, sort_keys=True))
        return

    typer.echo(f"date={date}")
    typer.echo(f"source={source}")
    typer.echo(f"artifacts_path={artifact_path}")
    typer.echo(f"count={len(artifacts)}")
    for artifact in artifacts:
        for line in _render_artifact(artifact):
            typer.echo(line)


def download_slack_artifact_bytes(
    date: str = typer.Option(
        ..., "--date", help="Date to download artifacts for in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    policy: str | None = typer.Option(
        None,
        "--policy",
        help="Override artifact download policy for this run.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Redownload even if the local blob already exists."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show how many source-native artifacts would download "
            "without writing blobs or metadata."
        ),
    ),
) -> None:
    """Download source-native Slack artifact bytes for one day."""

    load_dotenv()
    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_slack_config(paths)
    token = os.getenv(config.token_env_var)
    if not token:
        raise typer.BadParameter(
            f"Missing Slack token in environment variable {config.token_env_var}. "
            f"See {slack_config_path(paths)} or config/sources/slack.toml.example."
        )

    resolved_policy = policy or config.artifact_download_policy
    artifact_path = paths.artifact_metadata_path("slack", date)
    if not artifact_path.exists():
        raise typer.BadParameter(
            _missing_artifact_metadata_message(paths, source="slack", date=date)
        )

    if resolved_policy != "download-source-native":
        typer.echo("source=slack")
        typer.echo(f"date={date}")
        typer.echo(f"policy={resolved_policy}")
        typer.echo("download_mode=metadata-only")
        typer.echo(
            "next_step=use '--policy download-source-native' or update "
            "config/sources/slack.toml to fetch Slack-hosted files"
        )
        raise typer.Exit(code=0)

    result = download_slack_artifacts(
        paths,
        date=date,
        token=token,
        policy=resolved_policy,
        dry_run=dry_run,
        force=force,
    )

    typer.echo("source=slack")
    typer.echo(f"date={date}")
    typer.echo(f"policy={resolved_policy}")
    typer.echo(f"dry_run={str(dry_run).lower()}")
    typer.echo(f"artifacts_path={result.artifact_path}")
    typer.echo(f"artifacts_seen={result.artifacts_seen}")
    typer.echo(f"would_download={result.would_download}")
    typer.echo(f"downloaded={result.downloaded}")
    typer.echo(f"skipped_policy={result.skipped_policy}")
    typer.echo(f"skipped_existing={result.skipped_existing}")
    typer.echo(f"failed={result.failed}")

    if dry_run:
        typer.echo("next_step=run without --dry-run to download source-native Slack file blobs")
        return

    if result.downloaded > 0:
        typer.echo(f"next_step=run 'recall artifacts show --date {date}' to inspect local mirrors")
        return

    artifacts = read_jsonl(result.artifact_path)
    if any(_looks_like_slack_scope_error(artifact.get("last_error")) for artifact in artifacts):
        typer.echo(
            "hint=Slack file download may require the 'files:read' user scope. "
            "After adding it, reinstall or refresh the token and try again."
        )


def download_bluebubbles_artifact_bytes(
    date: str = typer.Option(
        ..., "--date", help="Date to download artifacts for in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    policy: str | None = typer.Option(None, "--policy", help="Override artifact download policy."),
    force: bool = typer.Option(False, "--force", help="Redownload even if local blob exists."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would download without writing blobs."
    ),
) -> None:
    """Download source-native BlueBubbles attachment bytes for one day."""

    load_dotenv()
    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    password = os.getenv(config.password_env_var)
    if not password:
        raise typer.BadParameter(
            f"Missing BlueBubbles password in environment variable {config.password_env_var}. "
            f"See {bluebubbles_config_path(paths)} or config/sources/bluebubbles.toml.example."
        )
    if not config.server_url:
        raise typer.BadParameter(
            f"Missing BlueBubbles server_url in {bluebubbles_config_path(paths)}."
        )

    resolved_policy = policy or config.artifact_download_policy
    artifact_path = paths.artifact_metadata_path("bluebubbles", date)
    if not artifact_path.exists():
        raise typer.BadParameter(
            _missing_artifact_metadata_message(paths, source="bluebubbles", date=date)
        )

    if resolved_policy != "download-source-native":
        typer.echo("source=bluebubbles")
        typer.echo(f"date={date}")
        typer.echo(f"policy={resolved_policy}")
        typer.echo("download_mode=metadata-only")
        typer.echo(
            "next_step=use '--policy download-source-native' or update "
            "config/sources/bluebubbles.toml to fetch BlueBubbles-hosted attachments"
        )
        raise typer.Exit(code=0)

    result = download_bluebubbles_artifacts(
        paths,
        date=date,
        server_url=config.server_url,
        password=password,
        policy=resolved_policy,
        dry_run=dry_run,
        force=force,
    )

    typer.echo("source=bluebubbles")
    typer.echo(f"date={date}")
    typer.echo(f"policy={resolved_policy}")
    typer.echo(f"dry_run={str(dry_run).lower()}")
    typer.echo(f"artifacts_path={result.artifact_path}")
    typer.echo(f"artifacts_seen={result.artifacts_seen}")
    typer.echo(f"would_download={result.would_download}")
    typer.echo(f"downloaded={result.downloaded}")
    typer.echo(f"skipped_policy={result.skipped_policy}")
    typer.echo(f"skipped_existing={result.skipped_existing}")
    typer.echo(f"failed={result.failed}")

    if dry_run:
        typer.echo(
            "next_step=run without --dry-run to fetch source-native BlueBubbles attachment bytes"
        )
        return

    if result.downloaded > 0:
        typer.echo(
            "next_step=run 'recall artifacts show --date "
            f"{date} --source bluebubbles' to inspect local mirrors"
        )
        return

    artifacts = read_jsonl(result.artifact_path)
    if any(
        _looks_like_bluebubbles_auth_error(artifact.get("last_error")) for artifact in artifacts
    ):
        typer.echo(
            "hint=BlueBubbles attachment download may require a correct server_url "
            "and server password. Confirm Recall on kit can reach bub and that "
            "the password matches the BlueBubbles server."
        )


def download_telegram_artifact_bytes(
    date: str = typer.Option(
        ..., "--date", help="Date to download artifacts for in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    policy: str | None = typer.Option(None, "--policy", help="Override artifact download policy."),
    force: bool = typer.Option(False, "--force", help="Redownload even if local blob exists."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would download without writing blobs."
    ),
) -> None:
    """Download source-native Telegram artifact bytes for one day."""

    load_dotenv()
    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)

    resolved_policy = policy or config.artifact_download_policy
    artifact_path = paths.artifact_metadata_path("telegram", date)
    if not artifact_path.exists():
        raise typer.BadParameter(
            _missing_artifact_metadata_message(paths, source="telegram", date=date)
        )

    if resolved_policy != "download-source-native":
        typer.echo("source=telegram")
        typer.echo(f"date={date}")
        typer.echo(f"policy={resolved_policy}")
        typer.echo("download_mode=metadata-only")
        typer.echo(
            "next_step=use '--policy download-source-native' or update "
            "config/sources/telegram.toml to fetch Telegram media bytes"
        )
        raise typer.Exit(code=0)

    tdlib_client = None
    tdlib_error: str | None = None
    if not dry_run:
        try:
            settings = build_tdlib_auth_settings(paths, config)
            transport = TdlibJsonTransport(
                library_path=settings.library_path,
                log_verbosity_level=settings.log_verbosity_level,
            )
            tdlib_client = TdlibTelegramClient(transport=transport, settings=settings)
        except Exception as exc:
            tdlib_error = str(exc)

    try:
        result = download_telegram_artifacts(
            paths,
            date=date,
            policy=resolved_policy,
            client=tdlib_client,
            dry_run=dry_run,
            force=force,
        )
    finally:
        if tdlib_client is not None:
            tdlib_client.close()

    typer.echo("source=telegram")
    typer.echo(f"date={date}")
    typer.echo(f"policy={resolved_policy}")
    typer.echo(f"dry_run={str(dry_run).lower()}")
    typer.echo(f"artifacts_path={result.artifact_path}")
    typer.echo(f"artifacts_seen={result.artifacts_seen}")
    typer.echo(f"would_download={result.would_download}")
    typer.echo(f"downloaded={result.downloaded}")
    typer.echo(f"skipped_policy={result.skipped_policy}")
    typer.echo(f"skipped_existing={result.skipped_existing}")
    typer.echo(f"failed={result.failed}")

    if dry_run:
        typer.echo("next_step=run without --dry-run to fetch Telegram media bytes")
        return

    if result.downloaded > 0:
        typer.echo(
            f"next_step=run 'recall artifacts show --date {date} --source telegram' "
            "to inspect local mirrors"
        )
        return

    if tdlib_error:
        typer.echo(
            "hint=Telegram artifact download can reuse existing local TDLib file paths, "
            "but TDLib credentials and session state may be needed for uncached media"
        )
        typer.echo(f"tdlib_error={tdlib_error}")
