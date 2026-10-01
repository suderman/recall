from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer

from recall.config import resolve_root
from recall.search import build_index, render_results, search
from recall.storage.paths import RecallPaths


def index(
    root: Path | None = typer.Option(None, "--root"),
    include_root: list[Path] | None = typer.Option(None, "--include-root"),
    index_path: Path | None = typer.Option(None, "--index"),
) -> None:
    """Rebuild local search; later included roots override exact repeated event IDs."""
    paths = [RecallPaths.from_root(resolve_root(root))]
    paths.extend(RecallPaths.from_root(path) for path in include_root or [])
    try:
        result = build_index(paths, index_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, sort_keys=True))


def query(
    text: str = typer.Argument("", help="Plain words to match; all words are required."),
    root: Path | None = typer.Option(None, "--root"),
    index_path: Path | None = typer.Option(None, "--index"),
    first: str | None = typer.Option(None, "--from"),
    last: str | None = typer.Option(None, "--to"),
    sources: list[str] | None = typer.Option(None, "--source"),
    identity: str | None = typer.Option(None, "--identity"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    relevance: bool = typer.Option(
        False, "--relevance", help="Rank text matches rather than newest."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Complete event records and citations for agents."
    ),
    as_org: bool = typer.Option(False, "--org", help="Quoted snippets with Org evidence links."),
) -> None:
    """Search captured evidence by words, date, source, or exact observed identity."""
    if as_json and as_org:
        raise typer.BadParameter("Choose either --json or --org")
    paths = RecallPaths.from_root(resolve_root(root))
    target = index_path or paths.derived / "search.sqlite3"
    try:
        rows = search(
            target,
            text,
            first=first,
            last=last,
            sources=sources,
            identity=identity,
            limit=limit,
            relevance=relevance,
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(rows, ensure_ascii=True, indent=2)
        if as_json
        else (render_results(rows, org=as_org))
    )
