from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa

from recall.storage.db import aliases, connect, identities, persons, resolutions
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class PersonRecord:
    person_id: str
    display_name: str
    sort_name: str | None
    tags: list[str]
    created_at: str


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    identity_id: str
    person_id: str | None
    source: str
    kind: str
    value: str
    label: str | None
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ResolutionRecord:
    resolution_id: str
    identity_id: str
    person_id: str
    confidence: str
    method: str
    valid_from: str | None
    valid_to: str | None
    evidence: list[str]
    created_at: str


def list_people(paths: RecallPaths) -> list[PersonRecord]:
    with connect(paths) as connection:
        rows = connection.execute(
            sa.select(
                persons.c.person_id,
                persons.c.display_name,
                persons.c.sort_name,
                persons.c.tags_json,
                persons.c.created_at,
            ).order_by(persons.c.display_name, persons.c.person_id)
        )
        return [
            PersonRecord(
                person_id=row.person_id,
                display_name=row.display_name,
                sort_name=row.sort_name,
                tags=json.loads(row.tags_json),
                created_at=row.created_at,
            )
            for row in rows
        ]


def list_identities(paths: RecallPaths, *, person_id: str | None = None) -> list[IdentityRecord]:
    statement = sa.select(
        identities.c.identity_id,
        identities.c.person_id,
        identities.c.source,
        identities.c.kind,
        identities.c.value,
        identities.c.label,
        identities.c.status,
        identities.c.created_at,
    ).order_by(identities.c.source, identities.c.kind, identities.c.value)
    if person_id is not None:
        statement = statement.where(identities.c.person_id == person_id)

    with connect(paths) as connection:
        rows = connection.execute(statement)
        return [IdentityRecord(*row) for row in rows]


def list_resolutions(paths: RecallPaths, *, person_id: str | None = None) -> list[ResolutionRecord]:
    statement = sa.select(
        resolutions.c.resolution_id,
        resolutions.c.identity_id,
        resolutions.c.person_id,
        resolutions.c.confidence,
        resolutions.c.method,
        resolutions.c.valid_from,
        resolutions.c.valid_to,
        resolutions.c.evidence_json,
        resolutions.c.created_at,
    ).order_by(resolutions.c.person_id, resolutions.c.identity_id, resolutions.c.method)
    if person_id is not None:
        statement = statement.where(resolutions.c.person_id == person_id)

    with connect(paths) as connection:
        rows = connection.execute(statement)
        return [
            ResolutionRecord(
                resolution_id=row.resolution_id,
                identity_id=row.identity_id,
                person_id=row.person_id,
                confidence=row.confidence,
                method=row.method,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                evidence=json.loads(row.evidence_json),
                created_at=row.created_at,
            )
            for row in rows
        ]


def list_person_aliases(
    paths: RecallPaths, *, person_id: str | None = None
) -> list[tuple[str, str, str]]:
    statement = sa.select(aliases.c.person_id, aliases.c.value, aliases.c.source).order_by(
        aliases.c.person_id, aliases.c.value, aliases.c.source
    )
    if person_id is not None:
        statement = statement.where(aliases.c.person_id == person_id)
    with connect(paths) as connection:
        return [(row.person_id, row.value, row.source) for row in connection.execute(statement)]
