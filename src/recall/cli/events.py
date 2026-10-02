from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from recall.config import resolve_root
from recall.dedupe import inspect_overlaps
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def show_overlaps(
    root: Path | None = typer.Option(None, "--root", resolve_path=True),
    source: list[str] | None = typer.Option(
        None,
        "--source",
        help="Saved source: telegram or bluebubbles. Repeat to select both.",
    ),
    account: str | None = typer.Option(None, "--account", help="Inspect only this source account."),
) -> None:
    """Report repeated raw source IDs as JSON without changing archive state."""
    try:
        report = inspect_overlaps(
            _paths_for(root), sources=source or ["telegram", "bluebubbles"], account=account
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True))


def _render_event(event: dict[str, Any]) -> list[str]:
    conversation_label = event.get("conversation_label") or "-"
    participants = ",".join(event.get("participant_identity_ids", [])) or "-"
    raw_ref = event.get("raw_ref")
    raw_ref_text = json.dumps(raw_ref, ensure_ascii=True, sort_keys=True) if raw_ref else "-"

    lines = [
        f"- {event['timestamp']} {event['source']}:{event['kind']} {conversation_label}",
        f"  text={event.get('text') or ''}",
        f"  sender={event.get('sender_identity_id') or '-'} participants={participants}",
        f"  raw_ref={raw_ref_text}",
    ]
    return lines


def show_events(
    date: str = typer.Option(..., "--date", help="Date to inspect in YYYY-MM-DD format."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum events to print."),
    as_json: bool = typer.Option(False, "--json", help="Print matching events as JSON."),
) -> None:
    """Show normalized events for one day."""

    paths = _paths_for(root)
    event_path = paths.normalized_event_path(date)
    if not event_path.exists():
        raise typer.BadParameter(f"No normalized event file found at {event_path}")

    events = read_jsonl(event_path)
    if limit is not None:
        events = events[:limit]

    if as_json:
        typer.echo(json.dumps(events, indent=2, ensure_ascii=True, sort_keys=True))
        return

    typer.echo(f"date={date}")
    typer.echo(f"events_path={event_path}")
    typer.echo(f"count={len(events)}")
    for event in events:
        for line in _render_event(event):
            typer.echo(line)
