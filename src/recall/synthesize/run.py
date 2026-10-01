"""Date-bounded orchestration using existing capture, replay, and draft checkpoints."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from recall.connectors.slack.backfill import backfill_slack
from recall.connectors.slack.capture import SlackCaptureClient
from recall.normalize.rebuild import SOURCES, date_range, rebuild_range
from recall.normalize.time import day_bounds
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize.generate import DEFAULT_MODEL, build_journals
from recall.synthesize.journal import _write_once


def run_journals(
    paths: RecallPaths,
    *,
    workspace: Path,
    first: str,
    last: str,
    sources: list[str],
    author: str,
    timezone_name: str = "America/Edmonton",
    model: str = DEFAULT_MODEL,
    slack_client: SlackCaptureClient | None = None,
    slack_account: str = "default",
    include_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield stage results; never capture remotely unless given an explicit Slack client."""
    days = date_range(first, last)
    day_bounds(first, timezone_name)
    if not sources or len(set(sources)) != len(sources) or set(sources) - set(SOURCES):
        raise ValueError(f"Choose distinct sources from {', '.join(SOURCES)}")
    if not author.strip():
        raise ValueError("Provide a journal author")
    if slack_client is not None and ("slack" not in sources or not slack_account.strip()):
        raise ValueError("Slack capture requires --source slack and a nonempty account")
    workspace = workspace.expanduser().resolve()
    if paths.root.is_relative_to(workspace) or workspace.is_relative_to(paths.root):
        raise ValueError("Run workspace must be separate from and not overlap the source root")
    protected = [
        part.resolve()
        for part in (
            paths.data,
            paths.config,
            paths.raw,
            paths.normalized,
            paths.artifacts,
            paths.entities,
            paths.derived,
            paths.state,
        )
    ]
    if any(workspace.is_relative_to(part) or part.is_relative_to(workspace) for part in protected):
        raise ValueError("Run workspace must not overlap source data or configuration")
    options = (
        json.dumps(
            {
                "format": "recall-journal-run-v1",
                "root": str(paths.root),
                "first": first,
                "last": last,
                "sources": sorted(sources),
                "timezone": timezone_name,
                "capture_slack": slack_client is not None,
                "slack_account": slack_account if slack_client is not None else None,
                "include_archived": include_archived if slack_client is not None else None,
            },
            sort_keys=True,
        )
        + "\n"
    )
    checkpoint = workspace / "run.json"
    if workspace.exists():
        if any(path.is_symlink() for path in workspace.rglob("*")):
            raise ValueError("Run workspace must not contain symlinks")
        if not checkpoint.exists() and any(path.name != "run.lock" for path in workspace.iterdir()):
            raise ValueError("Refusing an existing unowned run workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another journal run is using this workspace") from exc
        if checkpoint.exists() and checkpoint.read_text(encoding="utf-8") != options:
            raise ValueError("Run workspace has different inputs/options; choose a new workspace")
        _write_once(checkpoint, options)
        primary = RecallPaths.from_root(workspace / "replay")
        overlay = RecallPaths.from_root(workspace / "slack-replay")
        includes = []
        captured = RecallPaths.from_root(workspace / "capture")
        if slack_client is not None:
            rows = backfill_slack(
                paths,
                captured,
                client=slack_client,
                first=first,
                last=last,
                account=slack_account,
                timezone_name=timezone_name,
                include_archived=include_archived,
            )
            for row in rows:
                yield {**row, "stage": "capture", "source": "slack"}
            if any(row["status"] == "failed" for row in rows):
                raise ValueError("Slack capture failed; replay and journal generation stopped")
        jobs = rebuild_range(
            paths, primary, first=first, last=last, sources=sources, timezone_name=timezone_name
        )
        for row in jobs:
            yield {**row, "stage": "replay"}
        if any(row["status"] in {"failed", "unsupported"} for row in jobs):
            raise ValueError("Replay failed or is unsupported; journal generation stopped")
        if slack_client is not None:
            jobs = rebuild_range(
                captured,
                overlay,
                first=first,
                last=last,
                sources=["slack"],
                timezone_name=timezone_name,
                account=slack_account,
            )
            includes.append(overlay)
            for row in jobs:
                yield {**row, "stage": "replay"}
            if any(row["status"] in {"failed", "unsupported"} for row in jobs):
                raise ValueError(
                    "Slack replay failed or is unsupported; journal generation stopped"
                )
        for day in days:
            if not any(
                read_jsonl(root.normalized_event_path(day))
                for root in [primary, *includes]
                if root.normalized_event_path(day).exists()
            ):
                previous = workspace / "preview" / day[:4] / day[5:7] / f"{day}.org"
                yield {
                    "stage": "journal",
                    "date": day,
                    "status": "no-evidence",
                    "previous_preview": str(previous) if previous.exists() else None,
                }
                continue
            rows = build_journals(
                primary,
                first=day,
                last=day,
                author=author,
                output=workspace / "preview",
                timezone_name=timezone_name,
                model=model,
                include_roots=includes,
            )
            for row in rows:
                yield {**row, "stage": "journal"}
