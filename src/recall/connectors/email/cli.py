from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.email.entities import sync_email_entities as sync_email_entities_for_date
from recall.connectors.email.normalize import normalize_email_day
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def normalize_email(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    account: str = typer.Option("default", "--account", help="Email account label to record."),
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone for day selection."),
    query: str | None = typer.Option(None, "--query", help="Extra notmuch query filter."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Normalize one day of local email into daily JSONL events."""

    paths = _paths_for(root)
    paths.ensure_directories()
    normalized_path = normalize_email_day(
        paths, date=date, account=account, extra_query=query, timezone_name=timezone_name
    )
    typer.echo(f"Normalized email events for {date}")
    typer.echo(f"events={normalized_path}")
    typer.echo(f"next_step=run 'recall entities sync email --date {date}'")


def sync_email_entities(
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone for day selection."),
    date: str = typer.Option(
        ..., "--date", help="Date to sync identities from in YYYY-MM-DD format."
    ),
    query: str | None = typer.Option(None, "--query", help="Extra notmuch query filter."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Sync conversational email identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_email_entities_for_date(
        paths, date=date, extra_query=query, timezone_name=timezone_name
    )
    typer.echo(f"Synced email entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")
