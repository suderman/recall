from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

from recall.config import resolve_root
from recall.connectors.slack.artifacts import download_slack_artifacts
from recall.connectors.slack.config import load_slack_config, slack_config_path
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def _render_artifact(artifact: dict[str, Any]) -> list[str]:
    label = artifact.get("filename") or artifact.get("source_object_id") or "-"
    artifact_id = artifact.get("artifact_id") or "-"
    download_status = artifact.get("download_status") or "-"
    event_ids = ",".join(artifact.get("event_ids", [])) or "-"
    local_path = artifact.get("local_path") or "-"
    last_error = artifact.get("last_error") or "-"
    checksums = artifact.get("checksums") or {}
    checksum_text = ",".join(f"{name}:{value}" for name, value in sorted(checksums.items())) or "-"
    remote_locators = (
        ",".join(locator["kind"] for locator in artifact.get("remote_locators", [])) or "-"
    )
    raw_ref = artifact.get("raw_ref")
    raw_ref_text = json.dumps(raw_ref, ensure_ascii=True, sort_keys=True) if raw_ref else "-"

    lines = [
        f"- {artifact['source']}:{artifact['kind']} {label}",
        f"  artifact_id={artifact_id} download_status={download_status}",
        f"  event_ids={event_ids} remote_locators={remote_locators}",
        f"  local_path={local_path} checksums={checksum_text}",
        f"  last_error={last_error}",
        f"  raw_ref={raw_ref_text}",
    ]
    return lines


def show_artifacts(
    date: str = typer.Option(..., "--date", help="Date to inspect in YYYY-MM-DD format."),
    source: str = typer.Option("slack", "--source", help="Artifact source to inspect."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum artifacts to print."),
    as_json: bool = typer.Option(False, "--json", help="Print matching artifacts as JSON."),
) -> None:
    """Show normalized artifact metadata for one day."""

    paths = _paths_for(root)
    artifact_path = paths.artifact_metadata_path(source, date)
    if not artifact_path.exists():
        raise typer.BadParameter(f"No artifact metadata file found at {artifact_path}")

    artifacts = read_jsonl(artifact_path)
    if limit is not None:
        artifacts = artifacts[:limit]

    if as_json:
        typer.echo(json.dumps(artifacts, indent=2, ensure_ascii=True, sort_keys=True))
        return

    typer.echo(f"date={date}")
    typer.echo(f"source={source}")
    typer.echo(f"artifacts_path={artifact_path}")
    typer.echo(f"count={len(artifacts)}")
    for artifact in artifacts:
        for line in _render_artifact(artifact):
            typer.echo(line)


def download_slack_artifact_bytes(
    date: str = typer.Option(
        ..., "--date", help="Date to download artifacts for in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
    policy: str | None = typer.Option(
        None,
        "--policy",
        help="Override artifact download policy for this run.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Redownload even if the local blob already exists."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show how many source-native artifacts would download "
            "without writing blobs or metadata."
        ),
    ),
) -> None:
    """Download source-native Slack artifact bytes for one day."""

    load_dotenv()
    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_slack_config(paths)
    token = os.getenv(config.token_env_var)
    if not token:
        raise typer.BadParameter(
            f"Missing Slack token in environment variable {config.token_env_var}. "
            f"See {slack_config_path(paths)} or config/sources/slack.toml.example."
        )

    resolved_policy = policy or config.artifact_download_policy
    result = download_slack_artifacts(
        paths,
        date=date,
        token=token,
        policy=resolved_policy,
        dry_run=dry_run,
        force=force,
    )

    typer.echo("source=slack")
    typer.echo(f"date={date}")
    typer.echo(f"policy={resolved_policy}")
    typer.echo(f"dry_run={str(dry_run).lower()}")
    typer.echo(f"artifacts_path={result.artifact_path}")
    typer.echo(f"artifacts_seen={result.artifacts_seen}")
    typer.echo(f"would_download={result.would_download}")
    typer.echo(f"downloaded={result.downloaded}")
    typer.echo(f"skipped_policy={result.skipped_policy}")
    typer.echo(f"skipped_existing={result.skipped_existing}")
    typer.echo(f"failed={result.failed}")
