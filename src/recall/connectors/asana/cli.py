from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.asana.entities import sync_asana_entities as sync_asana_entities_for_date
from recall.connectors.asana.importer import import_asana_export
from recall.connectors.asana.normalize import normalize_asana_day
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def import_asana_export_bundle(
    export_path: Path = typer.Argument(..., exists=True, resolve_path=True),
    account: str = typer.Option("work", "--account", help="Asana account label to record."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Import an Asana export bundle into raw storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = import_asana_export(paths, export_path=export_path, account=account)
    typer.echo("mode=import")
    typer.echo(f"import_id={result.import_id}")
    typer.echo(f"import_dir={result.import_dir}")
    typer.echo(f"tasks_imported={result.tasks_imported}")
    typer.echo(f"stories_imported={result.stories_imported}")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo("next_step=run 'recall normalize asana --date YYYY-MM-DD' for each imported date")


def normalize_asana(
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
    """Normalize one day of imported Asana task history."""

    paths = _paths_for(root)
    paths.ensure_directories()
    event_path = normalize_asana_day(paths, date=date)
    typer.echo(f"Normalized Asana events for {date}")
    typer.echo(f"events={event_path}")
    typer.echo(f"next_step=run 'recall entities sync asana --date {date}'")


def sync_asana_entities(
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
    """Sync Asana identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_asana_entities_for_date(paths, date=date)
    typer.echo(f"Synced Asana entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")
