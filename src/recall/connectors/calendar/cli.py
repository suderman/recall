from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.calendar.normalize import normalize_calendar_day
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def normalize_calendar(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    account: str = typer.Option("default", "--account", help="Calendar account label to record."),
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone for day selection."),
    calendar: list[str] | None = typer.Option(
        None, "--calendar", help="Restrict to named khal calendars."
    ),
    include_canceled: bool = typer.Option(
        True,
        "--include-canceled/--exclude-canceled",
        help="Include canceled events in normalized output.",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        exists=False,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="Optional khal config file path.",
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
    """Normalize one day of local calendar data into daily JSONL events."""

    paths = _paths_for(root)
    paths.ensure_directories()
    normalized_path = normalize_calendar_day(
        paths,
        date=date,
        account=account,
        calendars=calendar,
        include_canceled=include_canceled,
        config_path=str(config_path) if config_path else None,
        timezone_name=timezone_name,
    )
    typer.echo(f"Normalized calendar events for {date}")
    typer.echo(f"events={normalized_path}")
    typer.echo(f"next_step=run 'recall events show --date {date}'")
