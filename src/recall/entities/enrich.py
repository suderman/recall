from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from recall.normalize.events import NormalizedEvent
from recall.storage.db import connect, identities, resolutions
from recall.storage.paths import RecallPaths


def _parse_event_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_lower_bound(value: str | None) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:
        return datetime.combine(
            date_cls.fromisoformat(value), datetime.min.time(), tzinfo=timezone.utc
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_upper_bound(value: str | None) -> tuple[datetime | None, bool]:
    if not value:
        return None, False
    if len(value) == 10:
        next_day = date_cls.fromisoformat(value) + timedelta(days=1)
        return datetime.combine(next_day, datetime.min.time(), tzinfo=timezone.utc), True
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc), False


def _window_matches(event_time: datetime, valid_from: str | None, valid_to: str | None) -> bool:
    lower = _parse_lower_bound(valid_from)
    upper, upper_is_exclusive = _parse_upper_bound(valid_to)
    if lower is not None and event_time < lower:
        return False
    if upper is None:
        return True
    if upper_is_exclusive:
        return event_time < upper
    return event_time <= upper


def _resolution_sort_key(row) -> tuple[str, str, str]:
    return (str(row.valid_from or ""), str(row.created_at or ""), str(row.person_id or ""))


def resolve_identity_person_id_at_time(
    connection: sa.Connection, identity_id: str, timestamp: str
) -> str | None:
    event_time = _parse_event_timestamp(timestamp)
    resolution_rows = connection.execute(
        sa.select(
            resolutions.c.person_id,
            resolutions.c.valid_from,
            resolutions.c.valid_to,
            resolutions.c.created_at,
        )
        .where(resolutions.c.identity_id == identity_id)
        .order_by(resolutions.c.created_at, resolutions.c.person_id)
    ).fetchall()
    matching_resolutions = [
        row for row in resolution_rows if _window_matches(event_time, row.valid_from, row.valid_to)
    ]
    if matching_resolutions:
        return sorted(matching_resolutions, key=_resolution_sort_key)[-1].person_id

    identity_row = connection.execute(
        sa.select(
            identities.c.person_id,
            identities.c.valid_from,
            identities.c.valid_to,
        ).where(identities.c.identity_id == identity_id)
    ).fetchone()
    if identity_row is None or identity_row.person_id is None:
        return None
    if not _window_matches(event_time, identity_row.valid_from, identity_row.valid_to):
        return None
    return identity_row.person_id


def enrich_events_with_people(
    paths: RecallPaths, events: list[NormalizedEvent]
) -> list[NormalizedEvent]:
    if not events or not paths.database.exists():
        return events

    with connect(paths) as connection:
        cache: dict[tuple[str, str], str | None] = {}
        for event in events:
            if event.sender_identity_id:
                key = (event.sender_identity_id, event.timestamp)
                if key not in cache:
                    cache[key] = resolve_identity_person_id_at_time(
                        connection, event.sender_identity_id, event.timestamp
                    )
                event.sender_person_id = cache[key]

            participant_person_ids: list[str] = []
            seen: set[str] = set()
            for identity_id in event.participant_identity_ids:
                key = (identity_id, event.timestamp)
                if key not in cache:
                    cache[key] = resolve_identity_person_id_at_time(
                        connection, identity_id, event.timestamp
                    )
                person_id = cache[key]
                if person_id is None or person_id in seen:
                    continue
                seen.add(person_id)
                participant_person_ids.append(person_id)
            event.participant_person_ids = participant_person_ids

    return events
