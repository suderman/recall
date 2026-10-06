from __future__ import annotations

import json
from pathlib import Path

import typer

from recall.config import resolve_root
from recall.storage.paths import RecallPaths
from recall.voice import candidates as inspect_candidates
from recall.voice_corpus import collect as collect_day
from recall.voice_corpus import inspect_day


def candidates(
    identity: list[str] = typer.Option(
        ..., "--identity", help="Explicit sender identity ID; repeat."
    ),
    first: str = typer.Option(..., "--from", help="First normalized day, inclusive."),
    last: str = typer.Option(..., "--to", help="Last normalized day, inclusive."),
    root: Path | None = typer.Option(None, "--root"),
    account: str | None = typer.Option(None, "--account", help="Limit to this source account."),
    source: str | None = typer.Option(
        None, "--source", help="Select email or telegram; Telegram requires an explicit account."
    ),
    limit: int = typer.Option(
        20, "--limit", min=1, max=200, help="Maximum selected records to show."
    ),
    exclude_event: list[str] | None = typer.Option(
        None, "--exclude-event", help="Known generated/unsuitable event ID to exclude; repeat."
    ),
) -> None:
    """Review candidates as JSON. Never confirm authorship, write a corpus or call a model."""
    try:
        report = inspect_candidates(
            RecallPaths.from_root(resolve_root(root)),
            identities=identity,
            first=first,
            last=last,
            account=account,
            source=source,
            limit=limit,
            exclude_events=exclude_event,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))


def collect(
    date: str = typer.Option(..., "--date", help="One saved normalized day."),
    source: str = typer.Option(..., "--source", help="Email or native Telegram."),
    account: str = typer.Option(..., "--account", help="Explicit source account."),
    policy: Path = typer.Option(..., "--policy", help="Private original-writing policy JSON."),
    coverage: Path | None = typer.Option(
        None, "--coverage", help="This output workspace's successful scoped replay manifest."
    ),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Reconcile one private local day scope. No capture, source query or model call."""
    try:
        report = collect_day(
            RecallPaths.from_root(resolve_root(root)),
            day=date,
            source=source,
            account=account,
            policy_path=policy,
            coverage_path=coverage,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))


def inspect(
    date: str = typer.Option(..., "--date", help="One saved voice day."),
    root: Path | None = typer.Option(None, "--root"),
    require_current: bool = typer.Option(
        False, "--require-current", help="Fail unless every scope is current."
    ),
) -> None:
    """Recheck saved policy and evidence without writing or printing private prose."""
    try:
        report = inspect_day(RecallPaths.from_root(resolve_root(root)), day=date)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    if require_current and not report["verified_current"]:
        raise typer.Exit(1)
