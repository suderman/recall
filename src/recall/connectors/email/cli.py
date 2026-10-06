from __future__ import annotations

import json
from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.email.entities import sync_email_entities as sync_email_entities_for_date
from recall.connectors.email.normalize import normalize_email_day
from recall.storage.paths import RecallPaths
from recall.voice_corpus import after_publication


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def normalize_email(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    account: str | None = typer.Option(
        None, "--account", help="Email account label (default: default)."
    ),
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone for day selection."),
    query: str | None = typer.Option(None, "--query", help="Extra notmuch query filter."),
    voice_policy: Path | None = typer.Option(
        None, "--voice-policy", help="Opt in to local voice collection after publication."
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
    """Normalize one day of local email into daily JSONL events."""

    if voice_policy is not None and (
        account is None or not account.strip() or account != account.strip()
    ):
        raise typer.BadParameter("Voice collection requires an explicit nonblank --account")
    account = "default" if account is None else account
    paths = _paths_for(root)
    paths.ensure_directories()
    normalized_path = normalize_email_day(
        paths, date=date, account=account, extra_query=query, timezone_name=timezone_name
    )
    typer.echo(f"Normalized email events for {date}")
    typer.echo(f"events={normalized_path}")
    typer.echo(f"next_step=run 'recall entities sync email --date {date}'")
    if voice_policy is not None:
        report = after_publication(
            paths, day=date, source="email", account=account, policy_path=voice_policy
        )
        typer.echo("voice=" + json.dumps(report, ensure_ascii=True, sort_keys=True))
        if not report["verified_current"]:
            raise typer.Exit(1)


def sync_email_entities(
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone for day selection."),
    date: str = typer.Option(
        ..., "--date", help="Date to sync identities from in YYYY-MM-DD format."
    ),
    query: str | None = typer.Option(None, "--query", help="Extra notmuch query filter."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Sync conversational email identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_email_entities_for_date(
        paths, date=date, extra_query=query, timezone_name=timezone_name
    )
    typer.echo(f"Synced email entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")
