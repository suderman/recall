from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from recall.connectors.slack.capture import (
    parse_date,
    raw_capture_paths,
)
from recall.normalize.artifacts import NormalizedArtifact, RemoteLocator
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_artifact_metadata, write_normalized_events
from recall.storage.paths import RecallPaths

URL_PATTERN = re.compile(r'https?://[^\s)>"]+')
SLACK_LINK_PATTERN = re.compile(r"<([^>|]+)(?:\|([^>]+))?>")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = index
            rows.append(row)
    return rows


def ts_to_iso(ts: str) -> str:
    text = str(ts)
    whole, _, fraction = text.partition(".")
    seconds = int(whole)
    microseconds = int((fraction + "000000")[:6])
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=microseconds)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ts_sort_key(value: str | None) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def slack_identity_id(message: dict[str, Any]) -> str | None:
    if message.get("user"):
        return f"ident_slack_{message['user']}"
    if message.get("bot_id"):
        return f"ident_slack_bot_{message['bot_id']}"
    return None


def link_values(text: str) -> list[str]:
    values: list[str] = []

    for match in SLACK_LINK_PATTERN.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://")):
            values.append(target)

    stripped = SLACK_LINK_PATTERN.sub(" ", text)
    for url in URL_PATTERN.findall(stripped):
        if url not in values:
            values.append(url)

    return values


def stable_event_id(account: str, conversation_id: str, ts: str) -> str:
    payload = f"slack:{account}:{conversation_id}:{ts}".encode("ascii")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def build_participant_index(
    conversations: dict[str, dict[str, Any]],
    message_rows: list[dict[str, Any]],
    auth_user_id: str | None,
) -> tuple[dict[str, list[str]], dict[tuple[str, str], list[str]]]:
    conversation_participants: dict[str, list[str]] = {}
    thread_participants: dict[tuple[str, str], list[str]] = {}
    conversation_sets: dict[str, set[str]] = defaultdict(set)
    thread_sets: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in message_rows:
        conversation_id = row["conversation_id"]
        conversation = conversations[conversation_id]
        identity_id = slack_identity_id(row["message"])
        if identity_id is not None:
            conversation_sets[conversation_id].add(identity_id)
            thread_key = (
                conversation_id,
                str(row["message"].get("thread_ts") or row["message"].get("ts")),
            )
            thread_sets[thread_key].add(identity_id)

        if conversation.get("kind") == "im" and conversation.get("other_user_id"):
            conversation_sets[conversation_id].add(f"ident_slack_{conversation['other_user_id']}")
        if conversation.get("kind") == "im" and auth_user_id:
            conversation_sets[conversation_id].add(f"ident_slack_{auth_user_id}")

    for conversation_id, identities in conversation_sets.items():
        conversation_participants[conversation_id] = sorted(identities)
    for thread_key, identities in thread_sets.items():
        thread_participants[thread_key] = sorted(identities)

    return conversation_participants, thread_participants


def stable_artifact_id(account: str, file_id: str) -> str:
    payload = f"slack:{account}:artifact:{file_id}".encode("ascii")
    return f"artifact_{hashlib.sha256(payload).hexdigest()[:20]}"


def artifact_remote_locators(file_object: dict[str, Any]) -> list[RemoteLocator]:
    locators: list[RemoteLocator] = []
    for key in ("url_private", "url_private_download", "permalink", "permalink_public"):
        value = file_object.get(key)
        if isinstance(value, str) and value:
            locators.append(RemoteLocator(kind=key, value=value))
    return locators


def message_artifact_records(
    *,
    account: str,
    conversation_id: str,
    event_id: str,
    timestamp: str,
    files: list[dict[str, Any]],
    raw_path: str,
    message_ts: str,
) -> list[NormalizedArtifact]:
    artifacts: list[NormalizedArtifact] = []

    for file_object in files:
        file_id = file_object.get("id")
        if not isinstance(file_id, str) or not file_id:
            continue

        raw_ref = RawReference(
            source="slack",
            path=raw_path,
            locator={
                "channel": conversation_id,
                "ts": message_ts,
                "file_id": file_id,
            },
        )
        artifacts.append(
            NormalizedArtifact(
                artifact_id=stable_artifact_id(account, file_id),
                source="slack",
                account=account,
                kind="file",
                source_object_id=file_id,
                event_ids=[event_id],
                remote_locators=artifact_remote_locators(file_object),
                local_path=None,
                mime_type=file_object.get("mimetype"),
                filename=file_object.get("name"),
                size_bytes=int(file_object["size"])
                if file_object.get("size") is not None
                else None,
                checksums={},
                download_status="not_requested",
                observed_at=timestamp,
                raw_ref=raw_ref,
            )
        )

    return artifacts


def normalize_slack_day(paths: RecallPaths, *, date: str) -> Path:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    if not metadata_path.exists() or not conversations_path.exists() or not messages_path.exists():
        raise FileNotFoundError(f"Missing Slack raw capture for {date} in {raw_dir}")

    metadata = load_json(metadata_path)
    conversations_list = load_json(conversations_path)
    messages = load_jsonl(messages_path)
    conversations = {conversation["id"]: conversation for conversation in conversations_list}
    account = str(metadata.get("account") or "default")

    conversation_participants, thread_participants = build_participant_index(
        conversations,
        messages,
        str(metadata.get("user_id")) if metadata.get("user_id") else None,
    )

    events: list[NormalizedEvent] = []
    artifacts: list[NormalizedArtifact] = []

    for row in sorted(
        messages,
        key=lambda item: (
            ts_sort_key(item["message"].get("thread_ts") or item["message"].get("ts")),
            ts_sort_key(item["message"].get("ts")),
            item["conversation_id"],
        ),
    ):
        message = row["message"]
        conversation = conversations[row["conversation_id"]]
        text = str(message.get("text") or "")
        thread_id = str(message.get("thread_ts") or message.get("ts"))
        if conversation["kind"] == "im":
            participant_identity_ids = conversation_participants.get(row["conversation_id"], [])
        else:
            participant_identity_ids = thread_participants.get(
                (row["conversation_id"], thread_id)
            ) or conversation_participants.get(row["conversation_id"], [])

        raw_relative = paths.relative_to_root(messages_path)
        event_id = stable_event_id(account, row["conversation_id"], str(message["ts"]))
        raw_ref = RawReference(
            source="slack",
            path=raw_relative,
            locator={
                "channel": row["conversation_id"],
                "ts": str(message["ts"]),
            },
        )
        sender_identity_id = slack_identity_id(message)
        tags = ["message"]
        source_urls = link_values(text)
        event_artifacts = message_artifact_records(
            account=account,
            conversation_id=row["conversation_id"],
            event_id=event_id,
            timestamp=ts_to_iso(str(message["ts"])),
            files=[
                file_object
                for file_object in message.get("files", [])
                if isinstance(file_object, dict)
            ],
            raw_path=raw_relative,
            message_ts=str(message["ts"]),
        )
        artifact_ids = [artifact.artifact_id for artifact in event_artifacts]
        artifacts.extend(event_artifacts)

        events.append(
            NormalizedEvent(
                event_id=event_id,
                source="slack",
                account=account,
                timestamp=ts_to_iso(str(message["ts"])),
                date=date,
                kind="message",
                conversation_id=row["conversation_id"],
                conversation_label=conversation["label"],
                thread_id=thread_id,
                sender_identity_id=sender_identity_id,
                participant_identity_ids=participant_identity_ids,
                text=text,
                source_urls=source_urls,
                artifact_ids=artifact_ids,
                raw_ref=raw_ref,
                raw_fragment=None,
                tags=tags,
            )
        )

    write_artifact_metadata(paths, source="slack", date=date, artifacts=artifacts)
    return write_normalized_events(paths, date, events)
