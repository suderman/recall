from __future__ import annotations

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.slack.api import SlackApiClient
from recall.connectors.slack.backfill import backfill_slack
from recall.connectors.slack.config import load_slack_config
from recall.storage.paths import RecallPaths


def slack(
    first: str = typer.Option(..., "--from", help="First local date, inclusive."),
    last: str = typer.Option(..., "--to", help="Last local date, inclusive."),
    output_root: Path = typer.Option(..., "--output-root", help="Separate raw-capture workspace."),
    root: Path | None = typer.Option(None, "--root", help="Root containing Slack source settings."),
    account: str | None = typer.Option(
        None, "--account", help="Override the source account label."
    ),
    timezone_name: str = typer.Option("UTC", "--timezone", help="Timezone for daily API windows."),
    include_archived: bool | None = typer.Option(None, "--include-archived/--exclude-archived"),
) -> None:
    """Capture bounded Slack history without changing live cursors or existing evidence."""
    paths = RecallPaths.from_root(resolve_root(root))
    output = RecallPaths.from_root(output_root)
    load_dotenv(paths.root / ".env")
    config = load_slack_config(paths)
    token = os.getenv(config.token_env_var)
    if not token:
        raise typer.BadParameter(f"Missing Slack token in {config.token_env_var}")
    try:
        with SlackApiClient(token) as client:
            rows = backfill_slack(
                paths,
                output,
                client=client,
                first=first,
                last=last,
                account=account or config.account,
                timezone_name=timezone_name,
                include_archived=config.include_archived
                if include_archived is None
                else include_archived,
            )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    for row in rows:
        typer.echo(json.dumps(row, sort_keys=True))
    if any(row["status"] == "failed" for row in rows):
        raise typer.Exit(1)
