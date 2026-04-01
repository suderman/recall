from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from recall.storage.paths import RecallPaths

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SlackCaptureClient(Protocol):
    def auth_test(self) -> dict[str, Any]: ...

    def list_users(self) -> list[dict[str, Any]]: ...

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, Any]]: ...

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str
    ) -> list[dict[str, Any]]: ...

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SlackCaptureResult:
    raw_dir: Path
    metadata_path: Path
    conversations_path: Path
    messages_path: Path
    stats: dict[str, int]


def parse_date(value: str) -> date_type:
    if not DATE_PATTERN.match(value):
        raise ValueError(f"Invalid date: {value}")

    return datetime.strptime(value, "%Y-%m-%d").date()


def local_day_bounds(value: str) -> tuple[str, str]:
    target_date = parse_date(value)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    start = datetime.combine(target_date, time(0, 0, 0), tzinfo=local_tz)
    end = datetime.combine(target_date, time(23, 59, 59, 999000), tzinfo=local_tz)
    return (f"{start.timestamp():.3f}", f"{end.timestamp():.3f}")


def normalize_whitespace(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_user_display_name(user: dict[str, Any]) -> str:
    profile = user.get("profile", {})
    display = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id")
        or "unknown"
    )
    if user.get("deleted"):
        return f"{display} [deactivated]"
    return display


def build_user_lookup(users: list[dict[str, Any]]) -> dict[str, str]:
    return {user["id"]: build_user_display_name(user) for user in users if user.get("id")}


def resolve_user_name(
    user_id: str | None, user_lookup: dict[str, str], fallback: str | None = None
) -> str:
    if not user_id:
        return fallback or "unknown"
    return user_lookup.get(user_id, fallback or user_id)


def format_slack_text(text: str | None, user_lookup: dict[str, str]) -> str:
    if not text:
        return ""

    formatted = str(text)
    formatted = re.sub(
        r"<@([A-Z0-9]+)>",
        lambda match: f"@{resolve_user_name(match.group(1), user_lookup, match.group(1))}",
        formatted,
    )
    formatted = re.sub(r"<!date\^[^|>]+\|([^>]+)>", r"\1", formatted)
    formatted = re.sub(r"<([^>|]+)\|([^>]+)>", r"\2 (\1)", formatted)
    formatted = re.sub(r"<([^>]+)>", r"\1", formatted)
    return normalize_whitespace(formatted)


def conversation_kind(conversation: dict[str, Any]) -> str:
    if conversation.get("is_im"):
        return "im"
    if conversation.get("is_mpim"):
        return "mpim"
    if conversation.get("is_private"):
        return "private_channel"
    return "public_channel"


def conversation_label(conversation: dict[str, Any], user_lookup: dict[str, str]) -> str:
    if conversation.get("is_im"):
        return (
            f"DM:{resolve_user_name(conversation.get('user'), user_lookup, conversation.get('id'))}"
        )

    if conversation.get("is_mpim"):
        purpose = normalize_whitespace(conversation.get("purpose", {}).get("value"))
        if purpose:
            return f"MPIM:{purpose}"
        return f"MPIM:{conversation.get('name') or conversation.get('id')}"

    if conversation.get("is_private"):
        return f"#{conversation.get('name') or conversation.get('id')} (private)"

    return f"#{conversation.get('name') or conversation.get('id')}"


def should_expand_thread(message: dict[str, Any]) -> bool:
    return bool(message.get("reply_count") and message.get("ts"))


def should_keep_message(message: dict[str, Any]) -> bool:
    text = str(message.get("text") or "").strip()
    if not text:
        return False
    return "<!date^" not in text


def ts_sort_key(value: str | None) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def message_author(message: dict[str, Any], user_lookup: dict[str, str]) -> str:
    user_id = message.get("user")
    if user_id:
        return resolve_user_name(user_id, user_lookup, user_id)
    if message.get("bot_id"):
        return f"bot:{message['bot_id']}"
    if message.get("username"):
        return str(message["username"])
    return "unknown"


def raw_capture_paths(paths: RecallPaths, date: str) -> tuple[Path, Path, Path, Path]:
    raw_dir = paths.raw_capture_dir("slack", date)
    return (
        raw_dir,
        raw_dir / "metadata.json",
        raw_dir / "conversations.json",
        raw_dir / "messages.jsonl",
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True))
            handle.write("\n")


def capture_slack_day(
    paths: RecallPaths,
    *,
    client: SlackCaptureClient,
    date: str,
    account: str,
    include_archived: bool = False,
) -> SlackCaptureResult:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    oldest, latest = local_day_bounds(date)

    auth = client.auth_test()
    users = client.list_users()
    user_lookup = build_user_lookup(users)
    conversations = client.list_conversations(include_archived=True)

    stats = {
        "total_listed": len(conversations),
        "checked": 0,
        "active": 0,
        "skipped_archived": 0,
        "top_level_messages": 0,
        "stored_messages": 0,
        "threads_expanded": 0,
    }

    conversation_rows: list[dict[str, Any]] = []
    message_rows: list[dict[str, Any]] = []

    for conversation in conversations:
        if conversation.get("is_archived") and not include_archived:
            stats["skipped_archived"] += 1
            continue

        stats["checked"] += 1
        history = [
            message
            for message in client.fetch_history(conversation["id"], oldest=oldest, latest=latest)
            if should_keep_message(message)
        ]

        if not history:
            continue

        history.sort(key=lambda message: ts_sort_key(message.get("ts")))
        stats["active"] += 1
        stats["top_level_messages"] += len(history)

        label = conversation_label(conversation, user_lookup)
        kind = conversation_kind(conversation)

        conversation_rows.append(
            {
                "id": conversation["id"],
                "label": label,
                "kind": kind,
                "name": conversation.get("name"),
                "is_archived": bool(conversation.get("is_archived")),
                "is_im": bool(conversation.get("is_im")),
                "is_mpim": bool(conversation.get("is_mpim")),
                "is_private": bool(conversation.get("is_private")),
                "other_user_id": conversation.get("user"),
                "purpose": conversation.get("purpose", {}).get("value"),
                "top_level_message_count": len(history),
            }
        )

        for message in history:
            message_rows.append(
                {
                    "conversation_id": conversation["id"],
                    "conversation_label": label,
                    "conversation_kind": kind,
                    "captured_via": "history",
                    "parent_ts": None,
                    "message": message,
                }
            )

            if should_expand_thread(message):
                thread_rows = [
                    reply
                    for reply in client.fetch_replies(conversation["id"], ts=str(message["ts"]))
                    if reply.get("ts") != message.get("ts") and should_keep_message(reply)
                ]
                thread_rows.sort(key=lambda reply: ts_sort_key(reply.get("ts")))

                if thread_rows:
                    stats["threads_expanded"] += 1
                    for reply in thread_rows:
                        message_rows.append(
                            {
                                "conversation_id": conversation["id"],
                                "conversation_label": label,
                                "conversation_kind": kind,
                                "captured_via": "thread",
                                "parent_ts": message.get("ts"),
                                "message": reply,
                            }
                        )

    message_rows.sort(
        key=lambda row: (
            row["conversation_label"],
            ts_sort_key(row["message"].get("thread_ts") or row["message"].get("ts")),
            ts_sort_key(row["message"].get("ts")),
        )
    )
    stats["stored_messages"] = len(message_rows)

    metadata = {
        "format_version": 1,
        "source": "slack",
        "account": account,
        "date": date,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "team": auth.get("team"),
        "team_id": auth.get("team_id"),
        "user": auth.get("user"),
        "user_id": auth.get("user_id"),
        "oldest": oldest,
        "latest": latest,
        "users": user_lookup,
        "stats": stats,
    }

    conversation_rows.sort(key=lambda row: (row["label"], row["id"]))
    write_json(metadata_path, metadata)
    write_json(conversations_path, conversation_rows)
    write_jsonl(messages_path, message_rows)

    return SlackCaptureResult(
        raw_dir=raw_dir,
        metadata_path=metadata_path,
        conversations_path=conversations_path,
        messages_path=messages_path,
        stats=stats,
    )


def render_capture_markdown(
    *,
    date: str,
    account: str,
    metadata: dict[str, Any],
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> str:
    user_lookup = metadata.get("users", {})
    messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for row in messages:
        messages_by_conversation.setdefault(row["conversation_id"], []).append(row)

    lines = [
        f"# Slack capture for {date}",
        "",
        f"- account: {account}",
        f"- workspace: {metadata.get('team')}",
        f"- user: {resolve_user_name(metadata.get('user_id'), user_lookup, metadata.get('user'))}",
        f"- oldest: {metadata.get('oldest')}",
        f"- latest: {metadata.get('latest')}",
        f"- conversations_checked: {metadata.get('stats', {}).get('checked')}",
        f"- conversations_active: {metadata.get('stats', {}).get('active')}",
        "",
    ]

    for conversation in conversations:
        lines.extend(
            [
                f"## {conversation['label']}",
                f"- id: {conversation['id']}",
                f"- type: {conversation['kind']}",
                "",
            ]
        )
        for row in messages_by_conversation.get(conversation["id"], []):
            message = row["message"]
            lines.append(
                f"- {message.get('ts')} {message_author(message, user_lookup)}: "
                f"{format_slack_text(message.get('text'), user_lookup)}"
            )
        lines.append("")

    return "\n".join(lines)
