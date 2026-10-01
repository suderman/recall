from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer

from recall import lookup
from recall.config import resolve_root
from recall.storage.paths import RecallPaths


def person(
    name: str = typer.Argument("", help="Match observed names; keep body-only mentions unlinked."),
    identity: str | None = typer.Option(None, "--identity"),
    root: Path | None = typer.Option(None, "--root"),
    index_path: Path | None = typer.Option(None, "--index"),
    first: str | None = typer.Option(None, "--from"),
    last: str | None = typer.Option(None, "--to"),
    sources: list[str] | None = typer.Option(None, "--source"),
    limit: int = typer.Option(10, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
    as_org: bool = typer.Option(False, "--org"),
) -> None:
    """Return observed identity candidates with cited history, without person merges."""
    if as_json and as_org:
        raise typer.BadParameter("Choose either --json or --org")
    paths = RecallPaths.from_root(resolve_root(root))
    target = index_path or paths.derived / "search.sqlite3"
    try:
        packet = lookup.person(
            target, name, identity=identity, first=first, last=last, sources=sources, limit=limit
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(packet, ensure_ascii=True, indent=2)
        if as_json
        else lookup.render_packet(packet, org=as_org)
    )


def project(
    text: str = typer.Argument(..., help="Project words to match; all words are required."),
    root: Path | None = typer.Option(None, "--root"),
    index_path: Path | None = typer.Option(None, "--index"),
    first: str | None = typer.Option(None, "--from"),
    last: str | None = typer.Option(None, "--to"),
    sources: list[str] | None = typer.Option(None, "--source"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
    as_org: bool = typer.Option(False, "--org"),
) -> None:
    """Return cited project matches, not inferred ownership or completion."""
    if as_json and as_org:
        raise typer.BadParameter("Choose either --json or --org")
    paths = RecallPaths.from_root(resolve_root(root))
    target = index_path or paths.derived / "search.sqlite3"
    try:
        packet = lookup.project(target, text, first=first, last=last, sources=sources, limit=limit)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(packet, ensure_ascii=True, indent=2)
        if as_json
        else lookup.render_packet(packet, org=as_org)
    )
