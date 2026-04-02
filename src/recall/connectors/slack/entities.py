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
    if kind == "email":
        normalized = value.strip().lower().replace("@", "_at_").replace(".", "_")
        return f"ident_slack_email_{normalized}"
    raise ValueError(f"Unsupported Slack identity kind: {kind}")


def _identity_alias_id(identity_id: str, value: str, source: str) -> str:
    payload = f"{identity_id}:{value}:{source}".encode("utf-8")
    return f"ialias_{hashlib.sha256(payload).hexdigest()[:20]}"


def _fallback_created_at(metadata: dict[str, Any], date: str) -> str:
    if metadata.get("captured_at"):
        return str(metadata["captured_at"])
    return f"{date}T00:00:00Z"


def _user_profiles(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = metadata.get("user_profiles")
    if isinstance(profiles, dict):
        return {
            str(user_id): profile
            for user_id, profile in profiles.items()
            if isinstance(profile, dict)
        }
    return {}


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
    user_profiles = _user_profiles(metadata)
    created_at = _fallback_created_at(metadata, date)

    identity_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    alias_seen: set[tuple[str, str, str]] = set()

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
            key = (identity_id, alias_value, "slack_user_profile")
            if key not in alias_seen:
                alias_seen.add(key)
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

        profile = user_profiles.get(user_id, {})
        email_value = str(profile.get("email") or "").strip().lower()
        if email_value:
            email_identity_id = _identity_id("email", email_value)
            identity_rows.append(
                {
                    "identity_id": email_identity_id,
                    "person_id": None,
                    "source": "slack",
                    "kind": "email",
                    "value": email_value,
                    "label": "Slack profile email",
                    "is_primary": False,
                    "status": "active",
                    "valid_from": None,
                    "valid_to": None,
                    "created_at": created_at,
                }
            )
            for alias_identity_id, source in (
                (identity_id, "slack_user_email"),
                (email_identity_id, "slack_user_email"),
            ):
                key = (alias_identity_id, email_value, source)
                if key in alias_seen:
                    continue
                alias_seen.add(key)
                alias_rows.append(
                    {
                        "identity_alias_id": _identity_alias_id(
                            alias_identity_id, email_value, source
                        ),
                        "identity_id": alias_identity_id,
                        "value": email_value,
                        "source": source,
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
