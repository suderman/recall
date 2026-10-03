from __future__ import annotations

import json
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.slack.api import SlackApiClient
from recall.connectors.slack.config import load_slack_config
from recall.storage.paths import RecallPaths
from recall.storage.references import resolve_reference
from recall.synthesize.generate import (
    DEFAULT_MODEL,
    _read_revision,
    build_journals,
    publish_journal,
)
from recall.synthesize.journal import inspect_packet, prepare_journal, save_journal
from recall.synthesize.run import run_journals


def run(
    first: str = typer.Option(..., "--from"),
    last: str = typer.Option(..., "--to"),
    author: str = typer.Option(..., "--author"),
    workspace: Path = typer.Option(..., "--workspace", help="Separate, dedicated run directory."),
    source: list[str] = typer.Option(..., "--source", help="Repeat for selected replay sources."),
    timezone_name: str = typer.Option("America/Edmonton", "--timezone"),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    capture_slack: bool = typer.Option(False, "--capture-slack", help="Opt into Slack API pulls."),
    slack_account: str | None = typer.Option(None, "--slack-account"),
    include_archived: bool | None = typer.Option(None, "--include-archived/--exclude-archived"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Capture optionally, replay selected sources, and generate previews. Never publish."""
    paths = RecallPaths.from_root(resolve_root(root))
    try:
        with ExitStack() as stack:
            client = None
            account = "default"
            archived = False
            if capture_slack:
                load_dotenv(paths.root / ".env")
                config = load_slack_config(paths)
                account = slack_account if slack_account is not None else config.account
                archived = config.include_archived if include_archived is None else include_archived
                token = os.getenv(config.token_env_var)
                if not token:
                    raise ValueError(f"Missing Slack token in {config.token_env_var}")
                client = stack.enter_context(SlackApiClient(token))
            elif slack_account is not None or include_archived is not None:
                raise ValueError("Slack capture options require --capture-slack")
            for row in run_journals(
                paths,
                workspace=workspace,
                first=first,
                last=last,
                sources=source,
                author=author,
                timezone_name=timezone_name,
                model=model,
                slack_client=client,
                slack_account=account,
                include_archived=archived,
            ):
                count = row.get("event_count", row.get("stored_messages"))
                suffix = f" ({count} events)" if count is not None else ""
                typer.echo(
                    f"{row['date']} {row['stage']} {row.get('source', '')}: {row['status']}{suffix}"
                )
                if row.get("error"):
                    typer.echo(row["error"], err=True)
                for limit in row.get("coverage_limits", []):
                    typer.echo(f"Capture limit: {limit}")
                if row.get("previous_preview"):
                    typer.echo(
                        f"Previous preview retained, no current evidence: {row['previous_preview']}"
                    )
                if row.get("path"):
                    typer.echo(f"Preview: {row['path']}")
                    typer.echo(f"Revision: {row['revision']}")
    except (ValueError, OSError, ZoneInfoNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


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


def inspect(
    packet: Path | None = typer.Option(None, "--packet"),
    revision: Path | None = typer.Option(None, "--revision"),
) -> None:
    """Check immutable packet/revision hashes and current citations. Never write or call a model."""
    if (packet is None) == (revision is None):
        raise typer.BadParameter("Choose exactly one of --packet or --revision")
    try:
        if revision is not None:
            record, _ = _read_revision(revision)
            packet = Path(record["packet"])
        assert packet is not None
        result = inspect_packet(packet)
        if revision is not None:
            result["revision"] = str(resolve_reference(revision))
        typer.echo(json.dumps(result, ensure_ascii=True, indent=2))
        if result["unresolved_citations"]:
            raise typer.Exit(1)
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


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
    packet: Path = typer.Option(..., "--packet", file_okay=False),
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
