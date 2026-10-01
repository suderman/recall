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
from recall.storage.state import ConnectorCursor, set_connector_cursor

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLACK_CURSOR_KEY = "latest_message_ts"


class SlackCaptureClient(Protocol):
    def auth_test(self) -> dict[str, Any]: ...

    def list_users(self) -> list[dict[str, Any]]: ...

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, Any]]: ...

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str, inclusive: bool = True
    ) -> list[dict[str, Any]]: ...

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SlackCaptureResult:
    raw_dir: Path
    metadata_path: Path
    conversations_path: Path
    messages_path: Path
    stats: dict[str, int]


@dataclass(frozen=True, slots=True)
class SlackWindowCapture:
    auth: dict[str, Any]
    account: str
    oldest: str
    latest: str
    users: dict[str, str]
    user_profiles: dict[str, dict[str, Any]]
    conversations: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    stats: dict[str, int]


@dataclass(frozen=True, slots=True)
class SlackIncrementalCaptureResult:
    account: str
    oldest: str
    latest: str
    cursor_before: str | None
    cursor_after: str | None
    dates_written: list[str]
    stored_messages: int
    cursor_updated: bool


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


def iso_datetime_to_slack_ts(value: str) -> str:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return f"{parsed.timestamp():.3f}"


def local_date_for_ts(value: str) -> str:
    timestamp = datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
    return timestamp.date().isoformat()


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


def build_user_profiles(users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        profile_value = user.get("profile")
        profile: dict[str, Any] = profile_value if isinstance(profile_value, dict) else {}
        profiles[str(user_id)] = {
            "display_name": build_user_display_name(user),
            "real_name": normalize_whitespace(
                profile.get("real_name") or user.get("real_name") or user.get("name")
            )
            or None,
            "email": normalize_whitespace(profile.get("email")) or None,
            "deleted": bool(user.get("deleted")),
        }
    return profiles


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


def ts_sort_key(value: str | None) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def max_ts(values: list[str]) -> str | None:
    if not values:
        return None
    return max(values, key=ts_sort_key)


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


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Slack JSON at {path}:{exc.lineno}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid Slack JSONL at {path}:{number}") from exc
    return rows


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


def collect_slack_window(
    client: SlackCaptureClient,
    *,
    account: str,
    oldest: str,
    latest: str,
    include_archived: bool = False,
    inclusive: bool = True,
) -> SlackWindowCapture:
    auth = client.auth_test()
    users = client.list_users()
    user_lookup = build_user_lookup(users)
    user_profiles = build_user_profiles(users)
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
        # Preserve raw messages, including attachment-only and scheduled details.
        history = client.fetch_history(
            conversation["id"], oldest=oldest, latest=latest, inclusive=inclusive
        )

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
                    if reply.get("ts") != message.get("ts")
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
    conversation_rows.sort(key=lambda row: (row["label"], row["id"]))

    return SlackWindowCapture(
        auth=auth,
        account=account,
        oldest=oldest,
        latest=latest,
        users=user_lookup,
        user_profiles=user_profiles,
        conversations=conversation_rows,
        messages=message_rows,
        stats=stats,
    )


def message_row_key(row: dict[str, Any]) -> tuple[str, str, str, str | None]:
    return (
        str(row["conversation_id"]),
        str(row["message"].get("ts")),
        str(row.get("captured_via") or "history"),
        str(row.get("parent_ts")) if row.get("parent_ts") is not None else None,
    )


def merge_message_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for row in existing_rows + new_rows:
        merged[message_row_key(row)] = row

    return sorted(
        merged.values(),
        key=lambda row: (
            row["conversation_label"],
            ts_sort_key(row["message"].get("thread_ts") or row["message"].get("ts")),
            ts_sort_key(row["message"].get("ts")),
        ),
    )


def merge_conversation_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    merged_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {row["id"]: row for row in existing_rows}
    for row in new_rows:
        merged[row["id"]] = row

    top_level_counts: dict[str, int] = {}
    for row in merged_messages:
        if row.get("captured_via") == "history":
            conversation_id = str(row["conversation_id"])
            top_level_counts[conversation_id] = top_level_counts.get(conversation_id, 0) + 1

    conversation_rows: list[dict[str, Any]] = []
    active_ids = {row["conversation_id"] for row in merged_messages}
    for conversation_id in sorted(active_ids):
        row = dict(merged[conversation_id])
        row["top_level_message_count"] = top_level_counts.get(conversation_id, 0)
        conversation_rows.append(row)

    conversation_rows.sort(key=lambda row: (row["label"], row["id"]))
    return conversation_rows


def build_day_metadata(
    *,
    date: str,
    account: str,
    auth: dict[str, Any],
    users: dict[str, str],
    user_profiles: dict[str, dict[str, Any]],
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    message_ts = [str(row["message"]["ts"]) for row in messages]
    top_level_messages = sum(1 for row in messages if row.get("captured_via") == "history")
    threads_expanded = len(
        {
            str(row["parent_ts"])
            for row in messages
            if row.get("captured_via") == "thread" and row.get("parent_ts") is not None
        }
    )

    return {
        "format_version": 1,
        "source": "slack",
        "account": account,
        "date": date,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "team": auth.get("team"),
        "team_id": auth.get("team_id"),
        "user": auth.get("user"),
        "user_id": auth.get("user_id"),
        "oldest": min(message_ts, key=ts_sort_key) if message_ts else None,
        "latest": max(message_ts, key=ts_sort_key) if message_ts else None,
        "users": users,
        "user_profiles": user_profiles,
        "stats": {
            "total_listed": len(conversations),
            "checked": len(conversations),
            "active": len({row["conversation_id"] for row in messages}),
            "skipped_archived": sum(1 for row in conversations if row.get("is_archived")),
            "top_level_messages": top_level_messages,
            "stored_messages": len(messages),
            "threads_expanded": threads_expanded,
        },
    }


def write_day_capture(
    paths: RecallPaths,
    *,
    date: str,
    account: str,
    auth: dict[str, Any],
    users: dict[str, str],
    user_profiles: dict[str, dict[str, Any]],
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> SlackCaptureResult:
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    metadata = build_day_metadata(
        date=date,
        account=account,
        auth=auth,
        users=users,
        user_profiles=user_profiles,
        conversations=conversations,
        messages=messages,
    )

    write_json(metadata_path, metadata)
    write_json(conversations_path, conversations)
    write_jsonl(messages_path, messages)

    return SlackCaptureResult(
        raw_dir=raw_dir,
        metadata_path=metadata_path,
        conversations_path=conversations_path,
        messages_path=messages_path,
        stats=metadata["stats"],
    )


def merge_day_capture(
    paths: RecallPaths,
    *,
    date: str,
    account: str,
    auth: dict[str, Any],
    users: dict[str, str],
    user_profiles: dict[str, dict[str, Any]],
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> SlackCaptureResult:
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    existing_metadata = read_json(metadata_path) if metadata_path.exists() else None
    existing_conversations = read_json(conversations_path) if conversations_path.exists() else []
    existing_messages = read_jsonl(messages_path) if messages_path.exists() else []

    merged_users = dict(existing_metadata.get("users", {}) if existing_metadata else {})
    merged_users.update(users)
    merged_user_profiles = dict(
        existing_metadata.get("user_profiles", {}) if existing_metadata else {}
    )
    merged_user_profiles.update(user_profiles)
    merged_messages = merge_message_rows(existing_messages, messages)
    merged_conversations = merge_conversation_rows(
        existing_conversations, conversations, merged_messages
    )

    return write_day_capture(
        paths,
        date=date,
        account=account,
        auth=auth,
        users=merged_users,
        user_profiles=merged_user_profiles,
        conversations=merged_conversations,
        messages=merged_messages,
    )


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
    oldest, latest = local_day_bounds(date)
    capture = collect_slack_window(
        client,
        account=account,
        oldest=oldest,
        latest=latest,
        include_archived=include_archived,
        inclusive=True,
    )
    return write_day_capture(
        paths,
        date=date,
        account=account,
        auth=capture.auth,
        users=capture.users,
        user_profiles=capture.user_profiles,
        conversations=capture.conversations,
        messages=capture.messages,
    )


def capture_slack_incremental(
    paths: RecallPaths,
    *,
    client: SlackCaptureClient,
    account: str,
    cursor_before: ConnectorCursor | None,
    since: str | None,
    until: str | None,
    include_archived: bool = False,
) -> SlackIncrementalCaptureResult:
    paths.ensure_directories()

    if cursor_before is None and since is None:
        raise ValueError(
            "Incremental capture requires an existing cursor or an explicit --since value"
        )

    if cursor_before is not None:
        oldest = cursor_before.cursor_value
    else:
        assert since is not None
        oldest = iso_datetime_to_slack_ts(since)
    latest = (
        iso_datetime_to_slack_ts(until)
        if until is not None
        else f"{datetime.now().timestamp():.3f}"
    )
    capture = collect_slack_window(
        client,
        account=account,
        oldest=oldest,
        latest=latest,
        include_archived=include_archived,
        inclusive=cursor_before is None,
    )

    messages_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in capture.messages:
        date = local_date_for_ts(str(row["message"]["ts"]))
        messages_by_date.setdefault(date, []).append(row)

    dates_written: list[str] = []
    for date in sorted(messages_by_date):
        day_messages = messages_by_date[date]
        conversation_ids = {row["conversation_id"] for row in day_messages}
        day_conversations = [row for row in capture.conversations if row["id"] in conversation_ids]
        merge_day_capture(
            paths,
            date=date,
            account=account,
            auth=capture.auth,
            users=capture.users,
            user_profiles=capture.user_profiles,
            conversations=day_conversations,
            messages=day_messages,
        )
        dates_written.append(date)

    observed_ts = max_ts([str(row["message"]["ts"]) for row in capture.messages])
    cursor_after = cursor_before.cursor_value if cursor_before is not None else None
    cursor_updated = False

    if observed_ts is not None and (
        cursor_after is None or ts_sort_key(observed_ts) > ts_sort_key(cursor_after)
    ):
        updated_cursor = set_connector_cursor(
            paths,
            source="slack",
            account=account,
            cursor_key=SLACK_CURSOR_KEY,
            cursor_value=observed_ts,
        )
        cursor_after = updated_cursor.cursor_value
        cursor_updated = True

    return SlackIncrementalCaptureResult(
        account=account,
        oldest=oldest,
        latest=latest,
        cursor_before=cursor_before.cursor_value if cursor_before is not None else None,
        cursor_after=cursor_after,
        dates_written=dates_written,
        stored_messages=capture.stats["stored_messages"],
        cursor_updated=cursor_updated,
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
