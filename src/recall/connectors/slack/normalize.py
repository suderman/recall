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
    format_slack_text,
    parse_date,
    raw_capture_paths,
)
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_normalized_events
from recall.storage.paths import RecallPaths

URL_PATTERN = re.compile(r'https?://[^\s)>"]+')


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
    return URL_PATTERN.findall(text)


def event_url(text: str) -> str | None:
    links = link_values(text)
    if len(links) == 1:
        return links[0]
    return None


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
    user_lookup = metadata.get("users", {})
    account = str(metadata.get("account") or "default")

    conversation_participants, thread_participants = build_participant_index(
        conversations,
        messages,
        str(metadata.get("user_id")) if metadata.get("user_id") else None,
    )

    events: list[NormalizedEvent] = []

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
        text = format_slack_text(message.get("text"), user_lookup)
        thread_id = str(message.get("thread_ts") or message.get("ts"))
        if conversation["kind"] == "im":
            participant_identity_ids = conversation_participants.get(row["conversation_id"], [])
        else:
            participant_identity_ids = thread_participants.get(
                (row["conversation_id"], thread_id)
            ) or conversation_participants.get(row["conversation_id"], [])

        raw_relative = paths.relative_to_root(messages_path)
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

        events.append(
            NormalizedEvent(
                event_id=stable_event_id(account, row["conversation_id"], str(message["ts"])),
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
                url=event_url(text),
                raw_ref=raw_ref,
                raw_fragment=None,
                tags=tags,
            )
        )

    return write_normalized_events(paths, date, events)
