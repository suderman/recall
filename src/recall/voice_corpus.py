"""Explicit local voice collection. Policy assertions are not AI-origin detection."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from recall.normalize.rebuild import date_range
from recall.storage.jsonl import write_text_atomic
from recall.storage.paths import RecallPaths
from recall.storage.references import raw_reference, resolve_reference
from recall.voice import candidates

POLICY_FORMAT = "recall-voice-policy-v1"
DAY_FORMAT = "recall-voice-day-v1"
VERSIONS = {"email": 2, "telegram": 1}
SCOPE_FIELDS = {"id", "source", "account", "identity", "from", "to", "conversations"}
SPAN_FIELDS = {"event_id", "body_sha256", "start", "end", "sha256", "extractor_version"}
GENERATED = {"generated", "ai_generated", "ai_assisted"}
AUTOMATED = {"automated", "bulk", "marketing", "transactional"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _parse(data: bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid saved voice JSON") from None


def _fields(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or (value.keys() - required - (optional or set()))
    ):
        raise ValueError("Invalid voice policy fields")


def _text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Expected a nonblank explicit policy value")


def _time(value: Any, *, utc_only: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected an aware timestamp")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or (utc_only and instant.utcoffset() != timedelta(0)):
        raise ValueError("Policy bounds must be UTC timestamps")
    return instant.astimezone(timezone.utc)


def _window(value: dict[str, Any]) -> None:
    if _time(value["from"], utc_only=True) > _time(value["to"], utc_only=True):
        raise ValueError("Reversed policy bounds")
    conversations = value["conversations"]
    if conversations is not None:
        if not isinstance(conversations, list) or not conversations:
            raise ValueError("Choose null or a nonempty conversation list")
        for conversation in conversations:
            _text(conversation)
        if len(set(conversations)) != len(conversations):
            raise ValueError("Duplicate conversation")


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Voice files and directories must be private and owner-owned")


def _read_private(path: Path) -> bytes:
    _private(path)
    return path.read_bytes()


def _load_policy(path: Path) -> tuple[dict[str, Any], bytes]:
    data = _read_private(path)
    policy = _parse(data)
    _fields(policy, {"format", "ownerships", "origin_grants", "denials", "exclude_events"})
    if policy["format"] != POLICY_FORMAT:
        raise ValueError("Unsupported voice policy")
    for key in ("ownerships", "origin_grants", "denials", "exclude_events"):
        if not isinstance(policy[key], list):
            raise ValueError("Expected policy lists")
    owners: dict[str, dict[str, Any]] = {}
    for key in ("ownerships", "denials"):
        ids: set[str] = set()
        for scope in policy[key]:
            _fields(scope, SCOPE_FIELDS)
            for name in ("id", "source", "account", "identity"):
                _text(scope[name])
            if scope["source"] not in VERSIONS or scope["id"] in ids:
                raise ValueError("Unsupported source or duplicate policy ID")
            ids.add(scope["id"])
            _window(scope)
            if key == "ownerships":
                owners[scope["id"]] = scope
    grant_ids: set[str] = set()
    for grant in policy["origin_grants"]:
        _fields(grant, {"id", "ownership_id", "from", "to", "conversations", "assertion"}, {"span"})
        _text(grant["id"])
        _text(grant["ownership_id"])
        if grant["id"] in grant_ids or grant["ownership_id"] not in owners:
            raise ValueError("Duplicate grant or unknown ownership")
        grant_ids.add(grant["id"])
        if grant["assertion"] != "original-human-unassisted":
            raise ValueError("Explicit original-human-unassisted assertion required")
        _window(grant)
        owner = owners[grant["ownership_id"]]
        if _time(grant["from"]) < _time(owner["from"]) or _time(grant["to"]) > _time(owner["to"]):
            raise ValueError("Origin grant exceeds ownership bounds")
        if owner["conversations"] is not None and (
            grant["conversations"] is None
            or not set(grant["conversations"]) <= set(owner["conversations"])
        ):
            raise ValueError("Origin grant exceeds ownership conversations")
        if "span" in grant:
            span = grant["span"]
            _fields(span, SPAN_FIELDS)
            _text(span["event_id"])
            for key in ("body_sha256", "sha256"):
                if not isinstance(span[key], str) or len(span[key]) != 64:
                    raise ValueError("Invalid span hash")
                int(span[key], 16)
            if any(type(span[key]) is not int for key in ("start", "end", "extractor_version")):
                raise ValueError("Invalid span offsets/version")
            if span["start"] < 0 or span["end"] <= span["start"] or span["extractor_version"] < 1:
                raise ValueError("Invalid span offsets/version")
    for event_id in policy["exclude_events"]:
        _text(event_id)
    if _read_private(path) != data:
        raise ValueError("Policy changed during read")
    return policy, data


def _matches(scope: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        all(scope[key] == event.get(key) for key in ("source", "account"))
        and scope["identity"] == event.get("sender_identity_id")
        and _within(scope, event)
    )


def _within(window: dict[str, Any], event: dict[str, Any]) -> bool:
    return _time(window["from"]) <= _time(event["timestamp"]) <= _time(window["to"]) and (
        window["conversations"] is None or event.get("conversation_id") in window["conversations"]
    )


def _scope(source: str, account: str, day: str) -> None:
    date_range(day, day)
    _text(account)
    if source not in VERSIONS:
        raise ValueError("Choose email or telegram")


def _saved_rows(data: bytes) -> list[dict[str, Any]]:
    if data and not data.endswith(b"\n"):
        raise ValueError("Incomplete saved JSONL snapshot")
    rows = [_parse(line) for line in data.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid saved JSONL record")
    return rows


def _coverage(
    paths: RecallPaths,
    *,
    day: str,
    source: str,
    account: str,
    path: Path,
    events: dict[str, tuple[int, bytes, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    """Check a successful local replay receipt, not remote history completeness."""
    state = paths.state / "rebuild"
    if path != state / "manifest.jsonl":
        raise ValueError("Coverage must come from this output workspace's replay manifest")
    data = _read_private(path)
    jobs = [row for row in _saved_rows(data) if (row["date"], row["source"]) == (day, source)]
    if len(jobs) != 1:
        raise ValueError("Missing or duplicate scoped replay receipt")
    job = jobs[0]
    options = job["options"]
    if (
        options["account"] != account
        or options["source"] != source
        or not isinstance(options["dates"], list)
        or day not in options["dates"]
    ):
        raise ValueError("Replay receipt requires the exact explicit account/day/source")
    allowed = {"success", "queried-empty" if source == "email" else "captured-empty"}
    if job["status"] not in allowed or job["error"] is not None:
        raise ValueError("Replay did not successfully publish this scope")
    watched = {path: data}
    snapshot = state / (
        f"email-{day}-events.jsonl" if source == "email" else "telegram-events.jsonl"
    )
    snapshot_data = _read_private(snapshot)
    expected_hash = job["query_snapshot_hash"] if source == "email" else job["events_hash"]
    if _hash(snapshot_data) != expected_hash:
        raise ValueError("Replay contribution snapshot changed")
    watched[snapshot] = snapshot_data
    expected = {}
    for row in _saved_rows(snapshot_data):
        if (row["source"], row.get("account")) != (source, account):
            raise ValueError("Replay snapshot outside receipt scope")
        if row["date"] == day:
            # Replay publication upserts the last observed record for each event ID.
            expected[row["event_id"]] = row
    if expected != {key: event for key, (_, _, event) in events.items()}:
        raise ValueError("Published contribution does not match replay snapshot")
    if type(job["event_count"]) is not int or job["event_count"] != len(expected):
        raise ValueError("Replay receipt count mismatch")
    if bool(expected) != (job["status"] == "success"):
        raise ValueError("Replay status does not match contribution")
    if source == "telegram":
        if not expected and job["capture_day_present"] is not True:
            raise ValueError("Empty replay lacks saved capture-day evidence")
        fingerprint = job["fingerprint"]
        _text(fingerprint)
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("Invalid replay fingerprint")
        input_path = state / "inputs" / (fingerprint + ".jsonl")
        if paths.root / job["input_manifest"] != input_path:
            raise ValueError("Invalid replay input manifest path")
        input_data = _read_private(input_path)
        inputs = _saved_rows(input_data)
        if len(inputs) != 1 or inputs[0]["fingerprint"] != fingerprint:
            raise ValueError("Replay input manifest mismatch")
        watched[input_path] = input_data
        hashes = inputs[0]["input_hashes"]
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError("Replay lacks saved input evidence")
        for name, digest in hashes.items():
            input_file = Path(name)
            if not input_file.is_absolute():
                raise ValueError("Replay input paths must be absolute")
            content = input_file.read_bytes()
            if _hash(content) != digest:
                raise ValueError("Replay input snapshot changed")
            watched[input_file] = content
    return {"path": str(path), "job_sha256": _hash(_json(job).encode())}, watched


def _evaluate(
    paths: RecallPaths,
    *,
    day: str,
    source: str,
    account: str,
    policy_path: Path,
    coverage_path: Path | None = None,
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    _scope(source, account, day)
    policy, policy_data = _load_policy(policy_path)
    recorded = paths.normalized_event_path(day)
    normalized = resolve_reference(recorded)
    data = normalized.read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError("Missing or incomplete normalized snapshot")
    events: dict[str, tuple[int, bytes, dict[str, Any]]] = {}
    for line, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line.strip():
            continue
        event = _parse(raw_line)
        if (
            not isinstance(event, dict)
            or any(
                not isinstance(event.get(key), str)
                for key in ("event_id", "source", "kind", "date", "timestamp")
            )
            or event["date"] != day
        ):
            raise ValueError("Invalid normalized event")
        _time(event["timestamp"])
        if (event["source"], event.get("account")) != (source, account):
            continue
        if event["event_id"] in events:
            raise ValueError("Duplicate normalized event ID")
        events[event["event_id"]] = line, raw_line, event
    watched = {policy_path: policy_data, normalized: data}
    coverage = None
    if coverage_path is not None:
        coverage, inputs = _coverage(
            paths, day=day, source=source, account=account, path=coverage_path, events=events
        )
        if any(path in watched and watched[path] != content for path, content in inputs.items()):
            raise ValueError("Input changed between snapshot reads")
        watched.update(inputs)
    if not events and coverage is None:
        raise ValueError("No selected source/account contribution")
    raw_inputs: dict[Path, bytes] = {}
    owners: dict[str, list[dict[str, Any]]] = {}
    blocked: dict[str, str] = {}
    identities: set[str] = set()
    for event_id, (_, _, event) in events.items():
        matches = [owner for owner in policy["ownerships"] if _matches(owner, event)]
        owners[event_id] = matches
        sender = event.get("sender_identity_id")
        if isinstance(sender, str) and sender:
            identities.add(sender)
        tags = event.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("Invalid event tags")
        if event_id in policy["exclude_events"]:
            blocked[event_id] = "explicitly_excluded"
        elif any(_matches(denial, event) for denial in policy["denials"]):
            blocked[event_id] = "origin_denied"
        elif not matches:
            blocked[event_id] = "identity_not_owned"
        elif len(matches) != 1:
            blocked[event_id] = "ambiguous_ownership"
        elif not (set(tags) & (GENERATED | AUTOMATED)) and event["kind"] == (
            "email" if source == "email" else "message"
        ):
            raw_path, error = raw_reference(event, recorded)
            if raw_path is None or error:
                raise ValueError("Unverifiable raw input")
            content = raw_path.read_bytes()
            if raw_path in watched and watched[raw_path] != content:
                raise ValueError("Raw input changed after receipt check")
            watched[raw_path] = raw_inputs[raw_path] = content
    report = (
        candidates(
            paths,
            identities=sorted(identities),
            first=day,
            last=day,
            source=source,
            account=account,
            exclude_events=list(blocked),
            limit=len(events) + 1,
        )
        if identities
        else {"records": [], "missing_days": [], "truncated": False}
    )
    if report["missing_days"] or report["truncated"]:
        raise ValueError("Incomplete candidate inspection")
    proposals = {item["event_id"]: item for item in report["records"]}
    records = []
    for event_id, (line, raw_line, event) in events.items():
        item = dict(
            proposals.get(
                event_id,
                {
                    "event_id": event_id,
                    "identity_id": event.get("sender_identity_id"),
                    "source": source,
                    "account": account,
                    "date": day,
                    "timestamp": event["timestamp"],
                    "line": line,
                    "raw_ref": event.get("raw_ref"),
                    "passage": None,
                    "exclusions": [],
                    "corpus_eligible": False,
                    "status": "excluded",
                },
            )
        )
        item["normalized_record_sha256"] = _hash(raw_line)
        item["conversation_id"] = event.get("conversation_id")
        item["extractor_version"] = VERSIONS[source]
        if event_id in blocked:
            item["status"], item["reason"] = "excluded", blocked[event_id]
        elif item.get("reason", "").startswith("raw_") or item.get("reason") in {
            "plain_body_unverifiable",
            "text_format_unverifiable",
        }:
            raise ValueError("Unverifiable candidate evidence")
        elif item["passage"] is not None:
            owner = owners[event_id][0]
            item["ownership_sha256"] = _hash(_json(owner).encode())
            span = {
                "event_id": event_id,
                "body_sha256": item["body_sha256"],
                "extractor_version": VERSIONS[source],
                **{key: item["passage"][key] for key in ("start", "end", "sha256")},
            }
            grants = [
                grant
                for grant in policy["origin_grants"]
                if grant["ownership_id"] == owner["id"]
                and _within(grant, event)
                and ("span" not in grant or grant["span"] == span)
            ]
            if len(grants) == 1:
                item["status"], item["reason"] = "eligible", "origin_granted"
                item["corpus_eligible"] = True
                item["origin_grant_sha256"] = _hash(_json(grants[0]).encode())
                item["span_id"] = _hash(
                    _json(
                        [
                            "recall-voice-span-v1",
                            source,
                            account,
                            event_id,
                            item["body_sha256"],
                            span["start"],
                            span["end"],
                            VERSIONS[source],
                        ]
                    ).encode()
                )
            elif grants:
                item["status"], item["reason"] = "excluded", "ambiguous_origin_grant"
            else:
                item["status"], item["reason"] = "needs_review", "origin_not_granted"
        if not item["corpus_eligible"]:
            item["passage"] = None
        records.append(item)
    scope = {
        "source": source,
        "account": account,
        "extractor_version": VERSIONS[source],
        "policy_path": str(policy_path),
        "policy_sha256": _hash(policy_data),
        "normalized_path": str(recorded),
        "resolved_normalized_path": str(normalized),
        "normalized_sha256": _hash(data),
        "raw_inputs": [
            {"path": str(path), "sha256": _hash(content)}
            for path, content in sorted(raw_inputs.items())
        ],
        "records": records,
    }
    if coverage is not None:
        scope["coverage"] = coverage
    _unchanged(watched, paths.state / "rebuild")
    return scope, watched


def _unchanged(watched: dict[Path, bytes], private_root: Path) -> None:
    for path, data in watched.items():
        if path.is_relative_to(private_root):
            _private(path)
        if path.read_bytes() != data:
            raise ValueError("Input changed during collection")


def _destination(paths: RecallPaths, day: str) -> Path:
    date_range(day, day)
    return paths.derived / "voice" / day[:4] / (day + ".json")


def _read_day(path: Path, day: str) -> tuple[dict[str, Any], bytes]:
    _private(path.parent, directory=True)
    _private(path.parent.parent, directory=True)
    data = _read_private(path)
    result = _parse(data)
    _fields(result, {"format", "date", "scopes"})
    if result["format"] != DAY_FORMAT or result["date"] != day or not result["scopes"]:
        raise ValueError("Invalid voice day result")
    if not isinstance(result["scopes"], list):
        raise ValueError("Invalid voice scopes")
    seen = set()
    for scope in result["scopes"]:
        if not isinstance(scope, dict):
            raise ValueError("Invalid voice scope")
        _scope(scope["source"], scope["account"], day)
        key = scope["source"], scope["account"]
        if key in seen:
            raise ValueError("Duplicate stored voice scope")
        seen.add(key)
    return result, data


def _directories(paths: RecallPaths, year: str) -> None:
    for path in (
        paths.data,
        paths.derived,
        paths.derived / "voice",
        paths.derived / "voice" / year,
    ):
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Voice output directories cannot be symlinks")
        if path.is_relative_to(paths.derived / "voice"):
            _private(path, directory=True)


def collect(
    paths: RecallPaths,
    *,
    day: str,
    source: str,
    account: str,
    policy_path: Path,
    coverage_path: Path | None = None,
) -> dict[str, Any]:
    """Replace one successfully checked scope; never turn failures into empty output."""
    try:
        policy_path = policy_path.expanduser().absolute()
        if coverage_path is not None:
            coverage_path = coverage_path.expanduser().absolute()
        scope, watched = _evaluate(
            paths,
            day=day,
            source=source,
            account=account,
            policy_path=policy_path,
            coverage_path=coverage_path,
        )
        target = _destination(paths, day)
        _directories(paths, day[:4])
        lock_path = paths.derived / "voice/.writer.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            _private(lock_path)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous, old = (
                _read_day(target, day)
                if target.exists() or target.is_symlink()
                else ({"format": DAY_FORMAT, "date": day, "scopes": []}, None)
            )
            retained = []
            event_ids = {item["event_id"] for item in scope["records"]}
            for entry in previous["scopes"]:
                if (entry["source"], entry["account"]) == (source, account):
                    # A valid JSONL prefix is not proof that omitted events were deleted.
                    if "coverage" not in scope and any(
                        item["event_id"] not in event_ids for item in entry["records"]
                    ):
                        raise ValueError("Event removal requires a coverage receipt")
                else:
                    retained.append(entry)
            value = {
                **previous,
                "scopes": sorted(
                    [*retained, scope], key=lambda entry: (entry["source"], entry["account"])
                ),
            }
            content = _json(value) + "\n"
            _private(policy_path)
            _unchanged(watched, paths.state / "rebuild")
            if content.encode() != old:
                write_text_atomic(target, content)
        return {
            "path": str(target),
            "date": day,
            "source": source,
            "account": account,
            "changed": content.encode() != old,
            "verified_current": True,
            "counts": {
                state: sum(item["status"] == state for item in scope["records"])
                for state in ("eligible", "needs_review", "excluded")
            },
        }
    except BlockingIOError:
        raise ValueError(
            "Voice collection not verified current: writer busy; no output replaced"
        ) from None
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        raise ValueError(
            "Voice collection not verified current; previous day output unchanged"
        ) from None


def after_publication(
    paths: RecallPaths,
    *,
    day: str,
    source: str,
    account: str,
    policy_path: Path,
    coverage_path: Path | None = None,
) -> dict[str, Any]:
    """Report voice failure separately; already published evidence stays published."""
    try:
        return collect(
            paths,
            day=day,
            source=source,
            account=account,
            policy_path=policy_path,
            coverage_path=coverage_path,
        )
    except ValueError as exc:
        return {
            "date": day,
            "source": source,
            "account": account,
            "verified_current": False,
            "error": str(exc),
        }


def inspect_day(paths: RecallPaths, *, day: str) -> dict[str, Any]:
    """Recheck current policy and saved evidence without writing or returning sample text."""
    target = _destination(paths, day)
    scopes = []
    try:
        saved, data = _read_day(target, day)
        watched = {target: data}
        policy_paths: set[Path] = set()
        for entry in saved["scopes"]:
            current = False
            try:
                policy_path = Path(entry["policy_path"])
                fresh, inputs = _evaluate(
                    paths,
                    day=day,
                    source=entry["source"],
                    account=entry["account"],
                    policy_path=policy_path,
                    coverage_path=Path(entry["coverage"]["path"]) if "coverage" in entry else None,
                )
                if any(
                    path in watched and watched[path] != content for path, content in inputs.items()
                ):
                    raise ValueError("Input changed between scope inspections")
                watched.update(inputs)
                policy_paths.add(policy_path)
                current = fresh == entry
            except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
                pass
            scopes.append(
                {
                    "source": entry["source"],
                    "account": entry["account"],
                    "verified_current": current,
                }
            )
        _private(target)
        for policy_path in policy_paths:
            _private(policy_path)
        _unchanged(watched, paths.state / "rebuild")
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return {"path": str(target), "date": day, "verified_current": False, "scopes": []}
    return {
        "path": str(target),
        "date": day,
        "verified_current": bool(scopes) and all(entry["verified_current"] for entry in scopes),
        "scopes": scopes,
    }
