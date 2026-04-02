from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.telegram.config import load_telegram_config
from recall.connectors.telegram.entities import (
    sync_telegram_entities as sync_telegram_entities_for_date,
)
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.storage.paths import RecallPaths
from recall.storage.state import list_connector_cursors


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def normalize_telegram(
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
    """Normalize one day of captured Telegram raw updates."""

    paths = _paths_for(root)
    paths.ensure_directories()
    event_path, artifact_path = normalize_telegram_day(paths, date=date)
    typer.echo(f"Normalized Telegram events for {date}")
    typer.echo(f"events={event_path}")
    typer.echo(f"artifacts={artifact_path}")
    typer.echo(
        f"next_step=run 'recall entities sync telegram --date {date}' or "
        f"'recall artifacts show --date {date} --source telegram'"
    )


def sync_telegram_entities(
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
    """Sync Telegram identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_telegram_entities_for_date(paths, date=date)
    typer.echo(f"Synced Telegram entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")


def show_telegram_state(
    account: str | None = typer.Option(
        None, "--account", help="Telegram account label to inspect."
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
    """Show stored Telegram cursor state for one account."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    cursors = list_connector_cursors(paths, source="telegram", account=resolved_account)

    typer.echo("source=telegram")
    typer.echo(f"account={resolved_account}")
    if not cursors:
        typer.echo("cursor_state=empty")
        return

    for cursor in cursors:
        typer.echo(f"cursor_key={cursor.cursor_key}")
        typer.echo(f"cursor_value={cursor.cursor_value}")
        typer.echo(f"updated_at={cursor.updated_at}")
