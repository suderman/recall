from __future__ import annotations

from pathlib import Path

import typer

from recall.config import resolve_root
from recall.entities.query import (
    list_identities,
    list_people,
    list_person_aliases,
    list_resolutions,
    list_unresolved_identities,
)
from recall.entities.resolve import match_entities
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def show_people(
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Show resolved people in SQLite entity storage."""

    paths = _paths_for(root)
    rows = list_people(paths)
    typer.echo(f"count={len(rows)}")
    for row in rows:
        typer.echo(f"- {row.person_id} {row.display_name}")
        if row.sort_name:
            typer.echo(f"  sort_name={row.sort_name}")
        aliases = [
            value for person_id, value, _ in list_person_aliases(paths, person_id=row.person_id)
        ]
        if aliases:
            typer.echo(f"  aliases={', '.join(aliases)}")


def show_identities(
    person_id: str | None = typer.Option(
        None, "--person-id", help="Filter identities by person id."
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
    """Show identities in SQLite entity storage."""

    paths = _paths_for(root)
    rows = list_identities(paths, person_id=person_id)
    typer.echo(f"count={len(rows)}")
    for row in rows:
        typer.echo(f"- {row.identity_id} {row.source}:{row.kind} value={row.value}")
        typer.echo(f"  person_id={row.person_id or '-'} status={row.status}")


def show_resolutions(
    person_id: str | None = typer.Option(
        None, "--person-id", help="Filter resolutions by person id."
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
    """Show identity-to-person resolutions in SQLite entity storage."""

    paths = _paths_for(root)
    rows = list_resolutions(paths, person_id=person_id)
    typer.echo(f"count={len(rows)}")
    for row in rows:
        typer.echo(f"- {row.identity_id} -> {row.person_id}")
        typer.echo(f"  method={row.method} confidence={row.confidence}")
        if row.valid_from or row.valid_to:
            typer.echo(f"  valid_from={row.valid_from or '-'} valid_to={row.valid_to or '-'}")
        if row.evidence:
            typer.echo(f"  evidence={'; '.join(row.evidence)}")


def show_unresolved_identities(
    suggested_only: bool = typer.Option(
        False,
        "--suggested-only",
        help="Only show unresolved identities with candidate person matches.",
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
    """Show unresolved identities and any proposed person matches."""

    paths = _paths_for(root)
    rows = list_unresolved_identities(paths, suggested_only=suggested_only)
    typer.echo(f"count={len(rows)}")
    for row in rows:
        typer.echo(f"- {row.identity_id} {row.source}:{row.kind} value={row.value}")
        if row.aliases:
            formatted_aliases = ", ".join(f"{value} ({source})" for value, source in row.aliases)
            typer.echo(f"  aliases={formatted_aliases}")
        if not row.suggested_matches:
            typer.echo("  suggested_matches=-")
            continue
        for suggestion in row.suggested_matches:
            typer.echo(
                f"  suggested={suggestion.person_id} {suggestion.display_name} "
                f"confidence={suggestion.confidence}"
            )
            if suggestion.evidence:
                typer.echo(f"  evidence={'; '.join(suggestion.evidence)}")


def run_entity_matching(
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Apply manual overrides and exact cross-source entity matching."""

    paths = _paths_for(root)
    result = match_entities(paths)
    typer.echo("Applied entity matching")
    typer.echo(f"manual_resolutions={result.manual_resolutions_applied}")
    typer.echo(f"automatic_resolutions={result.automatic_resolutions_applied}")
    typer.echo(f"person_merges={result.person_merges_applied}")
