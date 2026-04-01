from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from recall.connectors.slack.capture import raw_capture_paths
from recall.connectors.slack.normalize import load_json, load_jsonl
from recall.entities.storage import upsert_identities, upsert_identity_aliases
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class SlackEntitySyncResult:
    identities_synced: int
    aliases_synced: int


def _identity_id(kind: str, value: str) -> str:
    if kind == "user_id":
        return f"ident_slack_{value}"
    if kind == "bot_id":
        return f"ident_slack_bot_{value}"
    raise ValueError(f"Unsupported Slack identity kind: {kind}")


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _fallback_created_at(metadata: dict[str, Any], date: str) -> str:
    if metadata.get("captured_at"):
        return str(metadata["captured_at"])
    return f"{date}T00:00:00Z"


def _observed_user_ids(
    metadata: dict[str, Any],
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> set[str]:
    observed: set[str] = set()

    if metadata.get("user_id"):
        observed.add(str(metadata["user_id"]))

    for conversation in conversations:
        if conversation.get("other_user_id"):
            observed.add(str(conversation["other_user_id"]))

    for row in messages:
        message = row["message"]
        if message.get("user"):
            observed.add(str(message["user"]))

    return observed


def _observed_bot_ids(messages: list[dict[str, Any]]) -> set[str]:
    observed: set[str] = set()

    for row in messages:
        bot_id = row["message"].get("bot_id")
        if bot_id:
            observed.add(str(bot_id))

    return observed


def sync_slack_entities(paths: RecallPaths, *, date: str) -> SlackEntitySyncResult:
    paths.ensure_directories()
    raw_dir, metadata_path, conversations_path, messages_path = raw_capture_paths(paths, date)
    if not metadata_path.exists() or not conversations_path.exists() or not messages_path.exists():
        raise FileNotFoundError(f"Missing Slack raw capture for {date} in {raw_dir}")

    metadata = load_json(metadata_path)
    conversations = load_json(conversations_path)
    messages = load_jsonl(messages_path)
    user_lookup = metadata.get("users", {})
    created_at = _fallback_created_at(metadata, date)

    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []

    for user_id in sorted(_observed_user_ids(metadata, conversations, messages)):
        identity_id = _identity_id("user_id", user_id)
        identity_rows.append(
            {
                "identity_id": identity_id,
                "person_id": None,
                "source": "slack",
                "kind": "user_id",
                "value": user_id,
                "label": "Slack user ID",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": created_at,
            }
        )

        alias_value = str(user_lookup.get(user_id) or "").strip()
        if alias_value:
            alias_rows.append(
                {
                    "identity_alias_id": _identity_alias_id(
                        identity_id, alias_value, "slack_user_profile"
                    ),
                    "identity_id": identity_id,
                    "value": alias_value,
                    "source": "slack_user_profile",
                    "created_at": created_at,
                }
            )

    for bot_id in sorted(_observed_bot_ids(messages)):
        identity_rows.append(
            {
                "identity_id": _identity_id("bot_id", bot_id),
                "person_id": None,
                "source": "slack",
                "kind": "bot_id",
                "value": bot_id,
                "label": "Slack bot ID",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": created_at,
            }
        )

    identities_synced = upsert_identities(paths, identity_rows)
    aliases_synced = upsert_identity_aliases(paths, alias_rows)

    return SlackEntitySyncResult(
        identities_synced=identities_synced,
        aliases_synced=aliases_synced,
    )
