from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer

from recall.config import resolve_root
from recall.storage.paths import RecallPaths
from recall.synthesize.timeline import build_timelines


def build_timeline(
    first: str = typer.Option(..., "--from", help="First day, inclusive."),
    last: str = typer.Option(..., "--to", help="Last day, inclusive."),
    root: Path | None = typer.Option(None, "--root", help="Normalized output workspace."),
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA display timezone."),
) -> None:
    """Build deterministic Org evidence timelines. Refuse to overwrite edited views."""
    try:
        paths = build_timelines(
            RecallPaths.from_root(resolve_root(root)),
            first=first,
            last=last,
            timezone_name=timezone_name,
        )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for path in paths:
        typer.echo(str(path))
