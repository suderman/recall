from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def capture_slack(
    date: str = typer.Option(..., "--date", help="Date to capture in YYYY-MM-DD format."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Placeholder for Slack raw capture."""

    paths = _paths_for(root)
    paths.ensure_directories()
    raw_dir = paths.raw / "slack" / date
    typer.echo("Slack capture foundation is wired, but the connector is not implemented yet.")
    typer.echo(f"Target raw capture directory: {raw_dir}")
    raise typer.Exit(code=1)


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
    """Placeholder for Slack normalization."""

    paths = _paths_for(root)
    paths.ensure_directories()
    normalized_path = paths.normalized_event_path(date)
    typer.echo("Slack normalization foundation is wired, but the connector is not implemented yet.")
    typer.echo(f"Target normalized event path: {normalized_path}")
    raise typer.Exit(code=1)
