from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from recall.connectors.asana.capture import parse_date, raw_capture_paths
from recall.entities.enrich import enrich_events_with_people
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import read_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths


def _identity_id(kind: str, value: str) -> str:
    if kind == "user_id":
        return f"ident_asana_{value}"
    normalized = []
    for char in value.strip().lower():
        normalized.append(char if char.isalnum() else "_")
    compact = "".join(normalized).strip("_")
    while "__" in compact:
        compact = compact.replace("__", "_")
    return f"ident_asana_email_{compact}"


def _stable_event_id(account: str, event_type: str, primary_id: str) -> str:
    payload = f"asana:{account}:{event_type}:{primary_id}".encode("utf-8")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def _user_identity(user: dict[str, Any] | None) -> str | None:
    if not isinstance(user, dict):
        return None
    gid = str(user.get("gid") or "").strip()
    if gid:
        return _identity_id("user_id", gid)
    email = str(user.get("email") or "").strip().lower()
    if email:
        return _identity_id("email", email)
    return None


def _participant_identity_ids(task: dict[str, Any]) -> list[str]:
    participants: list[str] = []
    seen: set[str] = set()
    for user in [task.get("created_by"), task.get("assignee"), *(task.get("followers") or [])]:
        identity_id = _user_identity(user if isinstance(user, dict) else None)
        if identity_id is None or identity_id in seen:
            continue
        seen.add(identity_id)
        participants.append(identity_id)
    return participants


def _source_urls(task: dict[str, Any], text: str | None) -> list[str]:
    urls: list[str] = []
    permalink = str(task.get("permalink_url") or "").strip()
    if permalink:
        urls.append(permalink)
    if text:
        for token in text.split():
            if token.startswith("http://") or token.startswith("https://"):
                if token not in urls:
                    urls.append(token)
    return urls


def _task_text(task: dict[str, Any]) -> str | None:
    notes = str(task.get("notes") or "").strip()
    return notes or None


def _story_kind_and_tags(story: dict[str, Any]) -> tuple[str, list[str]]:
    story_type = str(story.get("type") or "story").strip() or "story"
    field_name = str(story.get("field_name") or "").strip().lower()
    tags = ["asana", "task_story", story_type]
    if story_type == "comment":
        tags.append("comment")
        return "task_story", tags
    if field_name in {"assignee", "assigned_to", "responsible_party"}:
        tags.append("assignment_change")
        return "task_assignment_change", tags
    if field_name in {"due_date", "due_on", "due_at"}:
        tags.append("due_date_change")
        return "task_due_date_change", tags
    if field_name in {"section", "memberships.section", "project_section"}:
        tags.append("section_change")
        return "task_section_change", tags
    if field_name in {"completed", "is_completed"}:
        tags.append("status_change")
        return "task_status_change", tags
    return "task_story", tags


def normalize_asana_day(paths: RecallPaths, *, date: str) -> Path:
    parse_date(date)
    paths.ensure_directories()
    raw_dir, events_path = raw_capture_paths(paths, date)
    if not events_path.exists():
        raise FileNotFoundError(f"Missing Asana raw capture for {date} in {raw_dir}")

    rows = read_jsonl(events_path)
    events: list[NormalizedEvent] = []
    raw_relative = paths.relative_to_root(events_path)

    for index, row in enumerate(rows, start=1):
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        account = str(row.get("account") or "default")
        timestamp = str(row.get("received_at") or f"{date}T00:00:00Z")

        if event_type == "task":
            task_gid = str(payload.get("gid") or f"line-{index}")
            text = _task_text(payload)
            events.append(
                NormalizedEvent(
                    event_id=_stable_event_id(account, event_type, task_gid),
                    source="asana",
                    account=account,
                    timestamp=timestamp,
                    date=date,
                    kind="task",
                    conversation_id=task_gid,
                    conversation_label=str(payload.get("name") or "").strip() or None,
                    thread_id=task_gid,
                    sender_identity_id=_user_identity(payload.get("created_by")),
                    participant_identity_ids=_participant_identity_ids(payload),
                    text=text,
                    source_urls=_source_urls(payload, text),
                    artifact_ids=[],
                    raw_ref=RawReference(
                        source="asana",
                        path=raw_relative,
                        locator={"line": index, "task_gid": task_gid},
                    ),
                    raw_fragment={
                        "task_gid": task_gid,
                        "name": payload.get("name"),
                        "notes": payload.get("notes"),
                        "completed": payload.get("completed"),
                        "completed_at": payload.get("completed_at"),
                        "due_on": payload.get("due_on"),
                        "projects": payload.get("projects"),
                    },
                    tags=["asana", "task"],
                )
            )
            continue

        if event_type == "task_completion":
            task_gid = str(payload.get("gid") or f"line-{index}")
            events.append(
                NormalizedEvent(
                    event_id=_stable_event_id(account, event_type, task_gid),
                    source="asana",
                    account=account,
                    timestamp=timestamp,
                    date=date,
                    kind="task_completion",
                    conversation_id=task_gid,
                    conversation_label=str(payload.get("name") or "").strip() or None,
                    thread_id=task_gid,
                    sender_identity_id=_user_identity(payload.get("completed_by")),
                    participant_identity_ids=_participant_identity_ids(payload),
                    text=str(payload.get("name") or "").strip() or None,
                    source_urls=_source_urls(payload, None),
                    artifact_ids=[],
                    raw_ref=RawReference(
                        source="asana",
                        path=raw_relative,
                        locator={"line": index, "task_gid": task_gid},
                    ),
                    raw_fragment={
                        "task_gid": task_gid,
                        "completed": payload.get("completed"),
                        "completed_at": payload.get("completed_at"),
                        "completed_by": payload.get("completed_by"),
                    },
                    tags=["asana", "task", "completed"],
                )
            )
            continue

        if event_type != "task_story":
            continue

        story = payload.get("story")
        if not isinstance(story, dict):
            continue
        task_gid = str(payload.get("task_gid") or f"line-{index}")
        story_gid = str(story.get("gid") or f"story-{index}")
        story_text = str(story.get("text") or "").strip() or None
        kind, tags = _story_kind_and_tags(story)
        story_type = str(story.get("type") or "story").strip() or "story"
        events.append(
            NormalizedEvent(
                event_id=_stable_event_id(account, event_type, story_gid),
                source="asana",
                account=account,
                timestamp=timestamp,
                date=date,
                kind=kind,
                conversation_id=task_gid,
                conversation_label=str(payload.get("task_name") or "").strip() or None,
                thread_id=task_gid,
                sender_identity_id=_user_identity(payload.get("actor")),
                participant_identity_ids=[
                    identity_id
                    for identity_id in [
                        _user_identity(payload.get("actor")),
                        _user_identity((payload.get("task_participants") or {}).get("assignee")),
                        _user_identity((payload.get("task_participants") or {}).get("created_by")),
                    ]
                    if identity_id is not None
                ],
                text=story_text,
                source_urls=_source_urls(story, story_text),
                artifact_ids=[],
                raw_ref=RawReference(
                    source="asana",
                    path=raw_relative,
                    locator={"line": index, "task_gid": task_gid, "story_gid": story_gid},
                ),
                raw_fragment={
                    "task_gid": task_gid,
                    "story_gid": story_gid,
                    "story_type": story_type,
                    "story_kind": kind,
                    "text": story.get("text"),
                    "field_name": story.get("field_name"),
                    "old_value": story.get("old_value"),
                    "new_value": story.get("new_value"),
                },
                tags=tags,
            )
        )

    enrich_events_with_people(paths, events)
    return write_normalized_events(paths, date, events, merge_existing=True)
