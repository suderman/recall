from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer

from recall.config import resolve_root
from recall.normalize.rebuild import rebuild_range
from recall.storage.paths import RecallPaths


def rebuild(
    first: str = typer.Option(..., "--from", help="First local event day, inclusive."),
    last: str = typer.Option(..., "--to", help="Last local event day, inclusive."),
    source: list[str] = typer.Option(..., "--source", help="Repeat for each selected source."),
    output_root: Path = typer.Option(..., "--output-root", help="Separate output workspace."),
    root: Path | None = typer.Option(None, "--root", help="Authoritative input workspace."),
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA event-day timezone."),
    account: str | None = typer.Option(None, "--account", help="Limit replacement to one account."),
    voice_policy: Path | None = typer.Option(
        None,
        "--voice-policy",
        help="Opt in to local collection after publication; requires --account.",
    ),
) -> None:
    """Rebuild from local evidence only. No remote capture or canonical-store writes."""
    try:
        jobs = rebuild_range(
            RecallPaths.from_root(resolve_root(root)),
            RecallPaths.from_root(output_root),
            first=first,
            last=last,
            sources=source,
            timezone_name=timezone_name,
            account=account,
            voice_policy=voice_policy,
        )
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for job in jobs:
        typer.echo(f"{job['date']} {job['source']}: {job['status']} ({job['event_count']} events)")
        if job["error"]:
            typer.echo(job["error"], err=True)
        if "voice" in job:
            typer.echo("voice=" + json.dumps(job["voice"], ensure_ascii=True, sort_keys=True))
    if any(job["status"] in {"failed", "unsupported"} for job in jobs) or (
        voice_policy is not None
        and any(not job.get("voice", {}).get("verified_current", False) for job in jobs)
    ):
        raise typer.Exit(1)
