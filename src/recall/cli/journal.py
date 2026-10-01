from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer

from recall.config import resolve_root
from recall.storage.paths import RecallPaths
from recall.synthesize.generate import DEFAULT_MODEL, build_journals, publish_journal
from recall.synthesize.journal import prepare_journal, save_journal


def build(
    first: str = typer.Option(..., "--from"),
    last: str = typer.Option(..., "--to"),
    author: str = typer.Option(..., "--author"),
    output: Path = typer.Option(Path("~/org/journal"), "--output"),
    timezone_name: str = typer.Option("America/Edmonton", "--timezone"),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    regenerate: bool = typer.Option(False, "--regenerate"),
    root: Path | None = typer.Option(None, "--root"),
    include_root: list[Path] | None = typer.Option(None, "--include-root"),
) -> None:
    """Generate cited journals through Pi; resume cached days and protect manual edits."""
    try:
        rows = build_journals(
            RecallPaths.from_root(resolve_root(root)),
            first=first,
            last=last,
            author=author,
            output=output,
            timezone_name=timezone_name,
            model=model,
            regenerate=regenerate,
            include_roots=[RecallPaths.from_root(path) for path in include_root or []],
        )
    except (ValueError, OSError, ZoneInfoNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for row in rows:
        typer.echo(f"{row['date']} {row['status']} {row['path']}")
        typer.echo(f"Revision: {row['revision']}")


def publish(
    revision: Path = typer.Option(..., "--revision", exists=True, dir_okay=False),
    output: Path = typer.Option(Path("~/org/journal"), "--output"),
    draft: Path | None = typer.Option(None, "--draft", exists=True, dir_okay=False),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Publish a checked revision or separately corrected body. Does not call a model."""
    try:
        result = publish_journal(
            RecallPaths.from_root(resolve_root(root)),
            revision=revision,
            output=output,
            draft=draft.read_text(encoding="utf-8") if draft is not None else None,
        )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{result['date']} {result['status']} {result['path']}")
    typer.echo(f"Revision: {result['revision']}")


def prepare(
    day: str = typer.Option(..., "--date"),
    author: str = typer.Option(..., "--author"),
    timezone_name: str = typer.Option("UTC", "--timezone"),
    root: Path | None = typer.Option(None, "--root"),
    include_root: list[Path] | None = typer.Option(None, "--include-root"),
) -> None:
    """Snapshot evidence and instructions locally. Does not call an LLM."""
    try:
        result = prepare_journal(
            RecallPaths.from_root(resolve_root(root)),
            day=day,
            author=author,
            timezone_name=timezone_name,
            include_roots=[RecallPaths.from_root(path) for path in include_root or []],
        )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(result)


def save(
    packet: Path = typer.Option(..., "--packet", exists=True, file_okay=False),
    draft: Path = typer.Option(..., "--draft", exists=True, dir_okay=False),
    model: str = typer.Option(..., "--model"),
    options: str = typer.Option("{}", "--options", help="Generation options as a JSON object."),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Save an externally drafted journal as a cited revision. No existing view is replaced."""
    try:
        generation_options = json.loads(options)
        if not isinstance(generation_options, dict):
            raise ValueError("Generation options must be a JSON object")
        result = save_journal(
            RecallPaths.from_root(resolve_root(root)),
            packet_dir=packet,
            body=draft.read_text(encoding="utf-8"),
            model=model,
            generation_options=generation_options,
        )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(result)
