from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.slack.api import SlackApiClient
from recall.connectors.slack.capture import capture_slack_day, raw_capture_paths
from recall.connectors.slack.config import load_slack_config, slack_config_path
from recall.connectors.slack.normalize import normalize_slack_day
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def capture_slack(
    date: str = typer.Option(..., "--date", help="Date to capture in YYYY-MM-DD format."),
    account: str | None = typer.Option(None, "--account", help="Slack account label to record."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    include_archived: bool | None = typer.Option(
        None,
        "--include-archived/--exclude-archived",
        help="Override whether archived conversations are captured.",
    ),
) -> None:
    """Capture one day of raw Slack data."""

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

    resolved_account = account or config.account
    archived = config.include_archived if include_archived is None else include_archived

    with SlackApiClient(token) as client:
        result = capture_slack_day(
            paths,
            client=client,
            date=date,
            account=resolved_account,
            include_archived=archived,
        )

    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    typer.echo(f"Captured Slack raw data for {date}")
    typer.echo(f"raw_dir={raw_dir}")
    typer.echo(f"metadata={metadata_path}")
    typer.echo(f"conversations={conversations_path}")
    typer.echo(f"messages={messages_path}")
    typer.echo(f"stored_messages={result.stats['stored_messages']}")


def normalize_slack(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Normalize one day of captured Slack data into daily JSONL events."""

    paths = _paths_for(root)
    paths.ensure_directories()
    normalized_path = normalize_slack_day(paths, date=date)
    typer.echo(f"Normalized Slack events for {date}")
    typer.echo(f"events={normalized_path}")
