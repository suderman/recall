from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.slack.api import SlackApiClient
from recall.connectors.slack.capture import (
    SLACK_CURSOR_KEY,
    SlackCaptureResult,
    SlackIncrementalCaptureResult,
    capture_slack_day,
    capture_slack_incremental,
    raw_capture_paths,
)
from recall.connectors.slack.config import load_slack_config, slack_config_path
from recall.connectors.slack.entities import sync_slack_entities as sync_slack_entities_for_date
from recall.connectors.slack.normalize import normalize_slack_day
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor, list_connector_cursors


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def capture_slack(
    date: str | None = typer.Option(None, "--date", help="Date to capture in YYYY-MM-DD format."),
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
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help="Capture messages newer than the stored Slack cursor for this account.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Seed incremental capture with an ISO-8601 timestamp when no cursor exists.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Stop capture at an ISO-8601 timestamp instead of now.",
    ),
) -> None:
    """Capture Slack raw data for a day or incrementally from cursor state."""

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

    if incremental and date is not None:
        raise typer.BadParameter("Use either --date or --incremental, not both")
    if not incremental and date is None:
        raise typer.BadParameter("Provide --date for bounded capture or use --incremental")

    incremental_result: SlackIncrementalCaptureResult | None = None
    day_result: SlackCaptureResult | None = None

    with SlackApiClient(token) as client:
        if incremental:
            cursor_before = get_connector_cursor(
                paths,
                source="slack",
                account=resolved_account,
                cursor_key=SLACK_CURSOR_KEY,
            )
            incremental_result = capture_slack_incremental(
                paths,
                client=client,
                account=resolved_account,
                cursor_before=cursor_before,
                since=since,
                until=until,
                include_archived=archived,
            )
        else:
            assert date is not None
            day_result = capture_slack_day(
                paths,
                client=client,
                date=date,
                account=resolved_account,
                include_archived=archived,
            )

    if incremental:
        assert incremental_result is not None
        typer.echo("mode=incremental")
        typer.echo(f"account={resolved_account}")
        typer.echo(f"cursor_key={SLACK_CURSOR_KEY}")
        typer.echo(f"cursor_before={incremental_result.cursor_before or '-'}")
        typer.echo(f"window_oldest={incremental_result.oldest}")
        typer.echo(f"window_latest={incremental_result.latest}")
        typer.echo(f"stored_messages={incremental_result.stored_messages}")
        typer.echo(
            "dates_written="
            + (
                ",".join(incremental_result.dates_written)
                if incremental_result.dates_written
                else "-"
            )
        )
        typer.echo(f"cursor_after={incremental_result.cursor_after or '-'}")
        typer.echo(f"cursor_updated={str(incremental_result.cursor_updated).lower()}")
        return

    assert date is not None
    assert day_result is not None
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    typer.echo("mode=day")
    typer.echo(f"Captured Slack raw data for {date}")
    typer.echo(f"raw_dir={raw_dir}")
    typer.echo(f"metadata={metadata_path}")
    typer.echo(f"conversations={conversations_path}")
    typer.echo(f"messages={messages_path}")
    typer.echo(f"stored_messages={day_result.stats['stored_messages']}")


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


def sync_slack_entities(
    date: str = typer.Option(
        ..., "--date", help="Date to sync identities from in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Sync Slack identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_slack_entities_for_date(paths, date=date)
    typer.echo(f"Synced Slack entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")


def show_slack_state(
    account: str | None = typer.Option(None, "--account", help="Slack account label to inspect."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Show stored Slack cursor state for one account."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_slack_config(paths)
    resolved_account = account or config.account
    cursors = list_connector_cursors(paths, source="slack", account=resolved_account)

    typer.echo("source=slack")
    typer.echo(f"account={resolved_account}")
    if not cursors:
        typer.echo("cursor_state=empty")
        return

    for cursor in cursors:
        typer.echo(f"cursor_key={cursor.cursor_key}")
        typer.echo(f"cursor_value={cursor.cursor_value}")
        typer.echo(f"updated_at={cursor.updated_at}")
