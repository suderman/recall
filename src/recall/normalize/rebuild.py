"""Bounded local replay. Authoritative inputs are never the output workspace."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from recall.connectors.asana.entities import sync_asana_entities
from recall.connectors.asana.normalize import normalize_asana_day
from recall.connectors.bluebubbles.entities import sync_bluebubbles_entities
from recall.connectors.bluebubbles.normalize import normalize_bluebubbles_day
from recall.connectors.calendar.normalize import normalize_calendar_day
from recall.connectors.email.entities import sync_email_entities
from recall.connectors.email.normalize import normalize_email_day
from recall.connectors.slack.entities import sync_slack_entities
from recall.connectors.slack.normalize import normalize_slack_day
from recall.connectors.telegram.entities import sync_telegram_entities
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.entities.observations import retain_labels
from recall.normalize.artifacts import NormalizedArtifact, RemoteLocator
from recall.normalize.events import NormalizedEvent, RawReference
from recall.normalize.time import day_bounds, event_date
from recall.storage.jsonl import (
    read_jsonl,
    write_artifact_metadata,
    write_jsonl,
    write_normalized_events,
)
from recall.storage.paths import RecallPaths

RAW_SOURCES = {
    "slack": (normalize_slack_day, sync_slack_entities, "messages.jsonl"),
    "bluebubbles": (normalize_bluebubbles_day, sync_bluebubbles_entities, "events.jsonl"),
    "telegram": (normalize_telegram_day, sync_telegram_entities, "updates.jsonl"),
    "asana": (normalize_asana_day, sync_asana_entities, "events.jsonl"),
}
SOURCES = (*RAW_SOURCES, "email", "calendar")


def date_range(first: str, last: str) -> list[str]:
    start, end = Date.fromisoformat(first), Date.fromisoformat(last)
    if end < start:
        raise ValueError("--to must be on or after --from")
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def event_from_record(row: dict[str, Any]) -> NormalizedEvent:
    data = dict(row)
    if data.get("raw_ref"):
        data["raw_ref"] = RawReference(**data["raw_ref"])
    return NormalizedEvent(**data)


def artifact_from_record(row: dict[str, Any]) -> NormalizedArtifact:
    data = dict(row)
    if data.get("raw_ref"):
        data["raw_ref"] = RawReference(**data["raw_ref"])
    data["remote_locators"] = [RemoteLocator(**value) for value in data["remote_locators"]]
    return NormalizedArtifact(**data)


def _snapshot_database(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)


def _raw_inputs(paths: RecallPaths, source: str) -> list[Path]:
    directory = paths.raw / source
    if not directory.exists():
        return []
    # Scan every saved capture day: late arrivals can live arbitrarily far from event day.
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_dir() and len(path.name) == 10 and _is_date(path.name)
    )


def _is_date(value: str) -> bool:
    try:
        Date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _validate_references(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        reference = row.get("raw_ref")
        if reference and not reference["path"].startswith("local:"):
            path = Path(reference["path"])
            if not path.is_absolute() or not path.is_file():
                raise ValueError(f"Unrecoverable raw reference for {row['event_id']}")


def _preserve_input_downloads(
    inputs: RecallPaths, source: str, artifacts: list[dict[str, Any]]
) -> None:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in artifacts:
        by_id.setdefault(row["artifact_id"], []).append(row)
    for path in sorted((inputs.artifacts_metadata / source).rglob("*.jsonl")):
        for previous in read_jsonl(path):
            if previous["download_status"] == "not_requested":
                continue
            for row in by_id.get(previous["artifact_id"], []):
                if row["download_status"] != "not_requested" and previous[
                    "download_status"
                ] not in {"downloaded", "imported"}:
                    continue
                for field in ("local_path", "checksums", "download_status", "last_error"):
                    row[field] = previous[field]
                if row["local_path"] and not Path(row["local_path"]).is_absolute():
                    row["local_path"] = str(inputs.root / row["local_path"])
                if row["size_bytes"] is None:
                    row["size_bytes"] = previous.get("size_bytes")


def _publish_day(
    output: RecallPaths,
    source: str,
    day: str,
    rows: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    account: str | None,
) -> None:
    destination = output.normalized_event_path(day)
    previous = read_jsonl(destination) if destination.exists() else []
    accounts = {row.get("account") for row in rows}
    if account is not None:
        accounts = {account}
    else:
        accounts.update(row.get("account") for row in previous if row["source"] == source)
    ids = {row["event_id"] for row in rows}
    selected = [
        artifact_from_record(row) for row in artifacts if ids.intersection(row["event_ids"])
    ]
    if selected:
        write_artifact_metadata(output, source=source, date=day, artifacts=selected)
    for selected_account in sorted(accounts, key=lambda value: value or ""):
        write_normalized_events(
            output,
            day,
            [event_from_record(row) for row in rows if row.get("account") == selected_account],
            replace_scope=(source, selected_account),
        )


def rebuild_range(
    inputs: RecallPaths,
    output: RecallPaths,
    *,
    first: str,
    last: str,
    sources: list[str],
    timezone_name: str = "UTC",
    account: str | None = None,
) -> list[dict[str, Any]]:
    """Replay saved captures and explicitly selected local queries, one writer at a time.

    Raw inputs are cached by content, code, options, and entity snapshot. Local
    queries are always repeated because their authoritative stores may change.
    A missing capture day is not an empty result, even after scanning late arrivals.
    Retain labels for published identities without publishing mutable resolution state.
    """
    days = date_range(first, last)
    day_bounds(first, timezone_name)
    if not sources or len(sources) != len(set(sources)) or set(sources) - set(SOURCES):
        raise ValueError(f"Choose distinct sources from {', '.join(SOURCES)}")
    if inputs.root.is_relative_to(output.root) or output.root.is_relative_to(inputs.root):
        raise ValueError("Input and output roots must be separate, non-overlapping workspaces")
    output.ensure_directories()
    state = output.state / "rebuild"
    state.mkdir(exist_ok=True)
    with (state / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _rebuild(inputs, output, days, sources, timezone_name, account, state)


def _rebuild(
    inputs: RecallPaths,
    output: RecallPaths,
    days: list[str],
    sources: list[str],
    timezone_name: str,
    account: str | None,
    state: Path,
) -> list[dict[str, Any]]:
    manifest_path = state / "manifest.jsonl"
    previous = read_jsonl(manifest_path) if manifest_path.exists() else []
    jobs = {(row["source"], row["date"]): row for row in previous}
    code = {
        path.relative_to(Path(__file__).parents[1]).as_posix(): file_hash(path)
        for path in sorted(Path(__file__).parents[1].rglob("*.py"))
    }
    with TemporaryDirectory(prefix="recall-rebuild-") as temporary:
        staging = RecallPaths.from_root(Path(temporary))
        staging.ensure_directories()
        _snapshot_database(inputs.database, staging.database)
        entity_hash = file_hash(staging.database) if staging.database.exists() else None
        config_hashes = {
            str(path): file_hash(path)
            for path in sorted(inputs.entity_resolution_config.glob("*.toml"))
        }
        # Only staging writes normalized/entity state; raw paths point to read-only inputs.
        if inputs.raw.exists():
            staging = replace(staging, raw=inputs.raw)
        for source in sources:
            capture_dirs = _raw_inputs(inputs, source) if source in RAW_SOURCES else []
            files = [
                path
                for directory in capture_dirs
                for path in sorted(directory.iterdir())
                if path.is_file() and path.suffix in {".json", ".jsonl"}
            ]
            files.extend(sorted((inputs.artifacts_metadata / source).rglob("*.jsonl")))
            input_hashes = {str(path): file_hash(path) for path in files}
            options = {
                "dates": days,
                "timezone": timezone_name,
                "account": account,
                "source": source,
                "root": str(inputs.root),
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    [options, input_hashes, entity_hash, config_hashes, code], sort_keys=True
                ).encode()
            ).hexdigest()
            input_manifest = state / "inputs" / f"{fingerprint}.jsonl"
            write_jsonl(
                input_manifest,
                [{"fingerprint": fingerprint, "input_hashes": input_hashes}],
            )
            cache = state / f"{source}-events.jsonl"
            artifact_cache = state / f"{source}-artifacts.jsonl"
            old = jobs.get((source, days[0]), {})
            cached = (
                source in RAW_SOURCES
                and old.get("fingerprint") == fingerprint
                and cache.exists()
                and artifact_cache.exists()
                and old.get("events_hash") == file_hash(cache)
                and old.get("artifacts_hash") == file_hash(artifact_cache)
            )
            failure = None
            unsupported_days: set[str] = set()
            try:
                if cached:
                    events, artifacts = read_jsonl(cache), read_jsonl(artifact_cache)
                    unsupported_days = set(old.get("unsupported_capture_days", []))
                elif source in RAW_SOURCES:
                    normalize, sync, filename = RAW_SOURCES[source]
                    for directory in capture_dirs:
                        sync(staging, date=directory.name)
                        normalize(staging, date=directory.name)
                        normalized = read_jsonl(staging.normalized_event_path(directory.name))
                        if read_jsonl(directory / filename) and not any(
                            row["source"] == source for row in normalized
                        ):
                            unsupported_days.add(directory.name)
                    events = []
                    for path in sorted(staging.normalized.rglob("*.jsonl")):
                        for row in read_jsonl(path):
                            if row["source"] != source:
                                continue
                            row["date"] = event_date(row["timestamp"], timezone_name)
                            if row["date"] in days and (
                                account is None or row.get("account") == account
                            ):
                                events.append(row)
                    artifacts = [
                        row
                        for path in sorted((staging.artifacts_metadata / source).rglob("*.jsonl"))
                        for row in read_jsonl(path)
                    ]
                    ids = {row["event_id"] for row in events}
                    artifacts = [row for row in artifacts if ids.intersection(row["event_ids"])]
                    _preserve_input_downloads(inputs, source, artifacts)
                    _validate_references(events)
                    write_jsonl(cache, events)
                    write_jsonl(artifact_cache, artifacts)
                else:
                    events, artifacts = [], []
            except Exception as exc:
                failure = type(exc).__name__ + ": " + str(exc)
                events, artifacts = [], []
            for day in days:
                status, error = "success", failure
                rows = [row for row in events if row["date"] == day]
                if failure:
                    status = "failed"
                elif source in RAW_SOURCES:
                    if day in unsupported_days:
                        status = "unsupported"
                    elif not rows and not any(directory.name == day for directory in capture_dirs):
                        status = "missing"
                    elif not rows:
                        status = "captured-empty"
                else:
                    try:
                        # Clear this source's staging contribution before a fresh local query.
                        write_normalized_events(
                            staging, day, [], replace_scope=(source, account or "default")
                        )
                        if source == "email":
                            sync_email_entities(staging, date=day, timezone_name=timezone_name)
                            normalize_email_day(
                                staging,
                                date=day,
                                account=account or "default",
                                timezone_name=timezone_name,
                            )
                        else:
                            normalize_calendar_day(
                                staging,
                                date=day,
                                account=account or "default",
                                timezone_name=timezone_name,
                            )
                        rows = [
                            row
                            for row in read_jsonl(staging.normalized_event_path(day))
                            if row["source"] == source
                        ]
                        _validate_references(rows)
                        if not rows:
                            status = "queried-empty"
                    except Exception as exc:
                        status, error = "failed", type(exc).__name__ + ": " + str(exc)
                snapshot_hash = None
                if source not in RAW_SOURCES and status in {"success", "queried-empty"}:
                    snapshot = state / f"{source}-{day}-events.jsonl"
                    write_jsonl(snapshot, rows)
                    snapshot_hash = file_hash(snapshot)
                job = {
                    "source": source,
                    "date": day,
                    "status": status,
                    "event_count": len(rows),
                    "error": error,
                    "fingerprint": fingerprint,
                    "options": options,
                    "input_manifest": output.relative_to_root(input_manifest),
                    "entity_hash": entity_hash,
                    "entity_config_hashes": config_hashes,
                    "code_hash": hashlib.sha256(
                        json.dumps(code, sort_keys=True).encode()
                    ).hexdigest(),
                    "query_snapshot_hash": snapshot_hash,
                    "unsupported_capture_days": sorted(unsupported_days),
                    "capture_day_present": any(path.name == day for path in capture_dirs),
                }
                if status in {"success", "captured-empty", "queried-empty"}:
                    _publish_day(output, source, day, rows, artifacts, account)
                    retain_labels(staging, output, rows)
                if (
                    source in RAW_SOURCES
                    and cache.exists()
                    and artifact_cache.exists()
                    and not failure
                ):
                    job.update(
                        events_hash=file_hash(cache), artifacts_hash=file_hash(artifact_cache)
                    )
                jobs[(source, day)] = job
                write_jsonl(manifest_path, [jobs[key] for key in sorted(jobs)])
    return [jobs[(source, day)] for day in days for source in sources]
