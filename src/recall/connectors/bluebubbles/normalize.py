from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.connectors.bluebubbles.capture import raw_capture_paths
from recall.normalize.artifacts import NormalizedArtifact, RemoteLocator
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_artifact_metadata, write_normalized_events
from recall.storage.paths import RecallPaths

URL_PATTERN = re.compile(r'https?://[^\s)>"]+')


def _load_raw_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = index
            rows.append(row)
    return rows


def _millis_to_iso(value: int | float | str | None, fallback: str) -> str:
    if value is None:
        return fallback

    numeric = float(value)
    if numeric > 1_000_000_000_000:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _identity_kind(value: str) -> str:
    if "@" in value:
        return "email"
    digits = value.replace("+", "").replace("-", "").replace(" ", "")
    if digits.isdigit():
        return "phone"
    return "handle"


def _identity_id(value: str) -> str:
    kind = _identity_kind(value)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"ident_bluebubbles_{kind}_{digest}"


def _event_id(account: str, conversation_id: str, message_guid: str) -> str:
    payload = f"bluebubbles:{account}:{conversation_id}:{message_guid}".encode("utf-8")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def _artifact_id(account: str, attachment_id: str) -> str:
    payload = f"bluebubbles:{account}:artifact:{attachment_id}".encode("utf-8")
    return f"artifact_{hashlib.sha256(payload).hexdigest()[:20]}"


def _source_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return URL_PATTERN.findall(text)


def _message_data(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload", {})
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _conversation_id(data: dict[str, Any]) -> str:
    if data.get("chatGuid"):
        return str(data["chatGuid"])
    chats = data.get("chats") or []
    if chats and isinstance(chats[0], dict) and chats[0].get("guid"):
        return str(chats[0]["guid"])
    return "unknown-chat"


def _conversation_label(data: dict[str, Any], conversation_id: str) -> str:
    return str(data.get("chatDisplayName") or data.get("displayName") or conversation_id)


def _participants(data: dict[str, Any]) -> list[str]:
    participants = data.get("participants") or []
    if isinstance(participants, list):
        values = [str(item) for item in participants if item]
    else:
        values = []
    handle = data.get("handle")
    if handle:
        values.append(str(handle))
    return sorted({_identity_id(value) for value in values})


def _sender_identity_id(data: dict[str, Any]) -> str | None:
    handle = data.get("handle")
    if handle:
        return _identity_id(str(handle))
    return None


def _attachment_artifacts(
    *,
    account: str,
    event_id: str,
    conversation_id: str,
    message_guid: str,
    timestamp: str,
    raw_path: str,
    attachments: list[dict[str, Any]],
) -> list[NormalizedArtifact]:
    artifacts: list[NormalizedArtifact] = []
    for index, attachment in enumerate(attachments):
        attachment_id = str(
            attachment.get("guid") or attachment.get("attachmentGuid") or f"{message_guid}:{index}"
        )
        raw_ref = RawReference(
            source="bluebubbles",
            path=raw_path,
            locator={
                "message_guid": message_guid,
                "attachment_id": attachment_id,
            },
        )
        locators: list[RemoteLocator] = []
        for key in ("path", "url", "transferName"):
            value = attachment.get(key)
            if isinstance(value, str) and value:
                kind = "attachment_path" if key == "path" else key
                locators.append(RemoteLocator(kind=kind, value=value))
        artifacts.append(
            NormalizedArtifact(
                artifact_id=_artifact_id(account, attachment_id),
                source="bluebubbles",
                account=account,
                kind="attachment",
                source_object_id=attachment_id,
                event_ids=[event_id],
                remote_locators=locators,
                mime_type=attachment.get("mimeType") or attachment.get("mime_type"),
                filename=attachment.get("filename") or attachment.get("transferName"),
                size_bytes=int(attachment["totalBytes"]) if attachment.get("totalBytes") else None,
                checksums={},
                download_status="not_requested",
                observed_at=timestamp,
                raw_ref=raw_ref,
            )
        )
    return artifacts


def normalize_bluebubbles_day(paths: RecallPaths, *, date: str) -> tuple[Path, Path]:
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    if not events_path.exists():
        raise FileNotFoundError(f"Missing BlueBubbles raw capture for {date} in {raw_dir}")

    rows = _load_raw_events(events_path)
    events: list[NormalizedEvent] = []
    artifacts: list[NormalizedArtifact] = []

    for row in rows:
        if row.get("event_type") not in {"new-message", "historical-message"}:
            continue

        data = _message_data(row)
        conversation_id = _conversation_id(data)
        conversation_label = _conversation_label(data, conversation_id)
        message_guid = str(data.get("guid") or f"line-{row['_line_number']}")
        timestamp = _millis_to_iso(data.get("dateCreated") or data.get("date"), row["received_at"])
        event_id = _event_id(str(row.get("account") or "personal"), conversation_id, message_guid)
        raw_path = paths.relative_to_root(events_path)
        raw_ref = RawReference(
            source="bluebubbles",
            path=raw_path,
            locator={
                "line": row["_line_number"],
                "message_guid": message_guid,
            },
        )
        text = str(data.get("text") or "")
        attachments = [item for item in (data.get("attachments") or []) if isinstance(item, dict)]
        event_artifacts = _attachment_artifacts(
            account=str(row.get("account") or "personal"),
            event_id=event_id,
            conversation_id=conversation_id,
            message_guid=message_guid,
            timestamp=timestamp,
            raw_path=raw_path,
            attachments=attachments,
        )
        artifacts.extend(event_artifacts)
        events.append(
            NormalizedEvent(
                event_id=event_id,
                source="bluebubbles",
                account=str(row.get("account") or "personal"),
                timestamp=timestamp,
                date=date,
                kind="message",
                conversation_id=conversation_id,
                conversation_label=conversation_label,
                thread_id=None,
                sender_identity_id=_sender_identity_id(data),
                participant_identity_ids=_participants(data),
                text=text,
                source_urls=_source_urls(text),
                artifact_ids=[artifact.artifact_id for artifact in event_artifacts],
                raw_ref=raw_ref,
                raw_fragment=None,
                tags=["message", "bluebubbles"],
            )
        )

    artifact_path = write_artifact_metadata(
        paths, source="bluebubbles", date=date, artifacts=artifacts
    )
    event_path = write_normalized_events(paths, date, events, merge_existing=True)
    return event_path, artifact_path
