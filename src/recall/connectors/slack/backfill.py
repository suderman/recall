from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from recall.connectors.slack.capture import (
    SlackCaptureClient,
    collect_slack_window,
    merge_message_rows,
    parse_date,
    write_day_capture,
)
from recall.normalize.time import day_bounds
from recall.storage.jsonl import read_jsonl, write_jsonl, write_text_atomic
from recall.storage.paths import RecallPaths

FORMAT = "recall-slack-backfill-v1"
FILES = ("metadata.json", "conversations.json", "messages.jsonl")
LIMITS = [
    "Only conversations visible to the configured user token are queried.",
    "Replies to parents outside the requested daily history window may be missing.",
    "Slack retention, deleted messages, and inaccessible conversations are not recoverable.",
]


def _checksums(directory: Path) -> dict[str, str]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES}


def _saved(directory: Path, options: dict[str, Any]) -> dict[str, Any] | None:
    if not directory.exists():
        return None
    proof = directory / "backfill.json"
    if not proof.is_file():
        raise ValueError(f"Refusing to replace an existing Slack capture: {directory}")
    try:
        row = json.loads(proof.read_text())
    except (ValueError, OSError) as exc:
        raise ValueError(f"Unreadable Slack capture checkpoint: {proof}") from exc
    if not isinstance(row, dict) or row.get("status") not in {"captured", "queried-empty"}:
        raise ValueError(f"Invalid Slack capture checkpoint: {proof}")
    if row.get("format") != FORMAT or row.get("options") != options:
        raise ValueError(f"Existing capture has different backfill options: {directory}")
    if _checksums(directory) != row.get("checksums"):
        raise ValueError(f"Saved Slack capture changed: {directory}")
    return row


def backfill_slack(
    paths: RecallPaths,
    output: RecallPaths,
    *,
    client: SlackCaptureClient,
    first: str,
    last: str,
    account: str,
    timezone_name: str,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Capture immutable daily bundles in a separate workspace, never live cursors."""
    if paths.root.is_relative_to(output.root) or output.root.is_relative_to(paths.root):
        raise ValueError("Backfill output must be separate from and not overlap the source root")
    start, stop = parse_date(first), parse_date(last)
    if start > stop or not account.strip():
        raise ValueError("Provide an account and an ordered inclusive date range")
    days = [(start + timedelta(days=n)).isoformat() for n in range((stop - start).days + 1)]
    day_bounds(first, timezone_name)  # Validate timezone before creating output.
    options = {"account": account, "timezone": timezone_name, "include_archived": include_archived}
    state = output.state / "backfill" / "slack.jsonl"
    lock_path = state.parent / "slack.lock"
    destinations = [output.raw / "slack", state, lock_path]
    destinations.extend(output.raw_capture_dir("slack", day) for day in days)
    protected = [
        part.resolve()
        for part in (paths.raw, paths.normalized, paths.artifacts, paths.state, paths.entities)
    ]
    for destination in destinations:
        resolved = destination.resolve()
        if not resolved.is_relative_to(output.root) or any(
            resolved.is_relative_to(part) for part in protected
        ):
            raise ValueError("Backfill paths must stay inside a separate output workspace")
    state.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Slack backfill is running in this output workspace") from exc
        jobs = {row["date"]: row for row in read_jsonl(state)} if state.exists() else {}
        if any(
            row.get("format") != FORMAT or row.get("options") != options for row in jobs.values()
        ):
            raise ValueError("Unrecognized or incompatible Slack backfill checkpoint file")
        # Published bundles also bind the workspace if the range checkpoint was lost.
        owned = {}
        for proof in (output.raw / "slack").glob("*/backfill.json"):
            if not proof.parent.resolve().is_relative_to(output.root):
                raise ValueError("Saved backfill must stay inside the output workspace")
            owned[proof.parent.name] = _saved(proof.parent, options)
        cached = {
            day: owned.get(day) or _saved(output.raw_capture_dir("slack", day), options)
            for day in days
        }
        workspaces = {
            tuple(row["workspace"])
            for row in [*jobs.values(), *owned.values()]
            if row and row.get("workspace")
        }
        if len(workspaces) > 1:
            raise ValueError("Output workspace contains conflicting Slack account identities")
        results = []
        for day in days:
            saved = cached[day]
            row: dict[str, Any]
            if saved is not None:
                row = {**saved, "reused": True}
            else:
                lower, upper = day_bounds(day, timezone_name)
                oldest = f"{lower.timestamp():.6f}"
                latest = f"{Decimal(str(upper.timestamp())) - Decimal('0.000001'):.6f}"
                row = {"format": FORMAT, "date": day, "options": options, "reused": False}
                try:
                    capture = collect_slack_window(
                        client,
                        account=account,
                        oldest=oldest,
                        latest=latest,
                        include_archived=include_archived,
                    )
                    workspace = (capture.auth.get("team_id"), capture.auth.get("user_id"))
                    if not all(workspace) or (workspaces and workspace not in workspaces):
                        raise ValueError("Slack token workspace/user differs from saved backfill")
                    workspaces.add(workspace)
                    messages = merge_message_rows(
                        [],
                        [
                            item
                            for item in capture.messages
                            if Decimal(oldest) <= Decimal(item["message"]["ts"]) <= Decimal(latest)
                        ],
                    )
                    ids = {item["conversation_id"] for item in messages}
                    conversations = [item for item in capture.conversations if item["id"] in ids]
                    directory = output.raw_capture_dir("slack", day)
                    directory.parent.mkdir(parents=True, exist_ok=True)
                    with TemporaryDirectory(
                        prefix=".slack-backfill-", dir=directory.parent
                    ) as name:
                        staging = RecallPaths.from_root(Path(name))
                        result = write_day_capture(
                            staging,
                            date=day,
                            account=account,
                            auth=capture.auth,
                            users=capture.users,
                            user_profiles=capture.user_profiles,
                            conversations=conversations,
                            messages=messages,
                        )
                        metadata = json.loads(result.metadata_path.read_text())
                        metadata["backfill_window"] = {
                            "oldest": oldest,
                            "latest": latest,
                            **options,
                        }
                        metadata["coverage_limits"] = LIMITS
                        metadata["collection_stats"] = capture.stats
                        write_text_atomic(
                            result.metadata_path, json.dumps(metadata, sort_keys=True) + "\n"
                        )
                        row.update(
                            status="captured" if messages else "queried-empty",
                            stored_messages=len(messages),
                            collection_stats=capture.stats,
                            workspace=list(workspace),
                            coverage_limits=LIMITS,
                            checksums=_checksums(result.raw_dir),
                        )
                        write_text_atomic(
                            result.raw_dir / "backfill.json", json.dumps(row, sort_keys=True) + "\n"
                        )
                        for file in (*FILES, "backfill.json"):
                            with (result.raw_dir / file).open("rb") as handle:
                                os.fsync(handle.fileno())
                        os.rename(result.raw_dir, directory)
                except Exception as exc:
                    row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            jobs[day] = row
            write_jsonl(state, [jobs[key] for key in sorted(jobs)])
            results.append(row)
            if row["status"] == "failed":
                break
        return results
