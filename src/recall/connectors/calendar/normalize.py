from __future__ import annotations

import hashlib
from pathlib import Path

from recall.connectors.calendar.khal import KhalRunner, load_calendar_events, parse_date
from recall.entities.enrich import enrich_events_with_people
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_normalized_events
from recall.storage.paths import RecallPaths


def _identity_id(address: str) -> str:
    normalized = []
    for char in address.strip().lower():
        normalized.append(char if char.isalnum() else "_")
    compact = "".join(normalized).strip("_")
    while "__" in compact:
        compact = compact.replace("__", "_")
    return f"ident_email_{compact}"


def _stable_event_id(account: str, occurrence_id: str) -> str:
    payload = f"calendar:{account}:{occurrence_id}".encode("utf-8")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def normalize_calendar_day(
    paths: RecallPaths,
    *,
    date: str,
    account: str = "default",
    calendars: list[str] | None = None,
    include_canceled: bool = True,
    config_path: str | None = None,
    runner: KhalRunner | None = None,
) -> Path:
    parse_date(date)
    paths.ensure_directories()
    records = load_calendar_events(
        date=date,
        calendars=calendars,
        include_canceled=include_canceled,
        config_path=config_path,
        runner=runner,
    )
    events: list[NormalizedEvent] = []
    for record in records:
        sender_identity_id = _identity_id(record.organizer) if record.organizer else None
        participant_identity_ids = [sender_identity_id] if sender_identity_id else []
        raw_ref = RawReference(
            source="calendar",
            path="local:khal",
            locator={
                "calendar": record.calendar,
                "uid": record.uid,
                "occurrence_id": record.occurrence_id,
            },
        )
        text = record.description or record.title or None
        events.append(
            NormalizedEvent(
                event_id=_stable_event_id(account, record.occurrence_id),
                source="calendar",
                account=account,
                timestamp=record.start,
                date=date,
                kind="calendar_event",
                conversation_id=record.occurrence_id,
                conversation_label=record.title or None,
                thread_id=record.series_id,
                sender_identity_id=sender_identity_id,
                participant_identity_ids=participant_identity_ids,
                text=text,
                source_urls=record.source_urls,
                artifact_ids=[],
                raw_ref=raw_ref,
                raw_fragment=record.raw_fragment,
                tags=list(record.tags),
            )
        )
    enrich_events_with_people(paths, events)
    return write_normalized_events(paths, date, events, merge_existing=True)
