from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa

from recall.storage.db import aliases, connect, identities, identity_aliases, persons, resolutions
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


@dataclass(frozen=True, slots=True)
class SuggestedPersonMatch:
    person_id: str
    display_name: str
    confidence: str
    evidence: list[str]


@dataclass(frozen=True, slots=True)
class UnresolvedIdentityRecord:
    identity_id: str
    source: str
    kind: str
    value: str
    created_at: str
    aliases: list[tuple[str, str]]
    suggested_matches: list[SuggestedPersonMatch]


def _norm_email(value: str) -> str:
    return value.strip().lower()


def _norm_phone(value: str) -> str:
    keep = [ch for ch in value if ch.isdigit() or ch == "+"]
    return "".join(keep)


def _norm_handle(value: str) -> str:
    return value.strip().lower().lstrip("@")


def _kind_bucket(
    source: str, kind: str, value: str, alias_source: str | None = None
) -> tuple[str, str] | None:
    del source
    if kind in {"email"}:
        return ("email", _norm_email(value))
    if kind in {"phone", "phone_number"}:
        return ("phone", _norm_phone(value))
    if kind in {"username", "handle"}:
        return ("handle", _norm_handle(value))
    if alias_source and alias_source.endswith("_email"):
        return ("email", _norm_email(value))
    if alias_source and alias_source.endswith("_username"):
        return ("handle", _norm_handle(value))
    if alias_source and alias_source.endswith("_phone_number"):
        return ("phone", _norm_phone(value))
    return None


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


def list_unresolved_identities(
    paths: RecallPaths,
    *,
    suggested_only: bool = False,
) -> list[UnresolvedIdentityRecord]:
    with connect(paths) as connection:
        person_lookup = {
            row.person_id: row.display_name
            for row in connection.execute(sa.select(persons.c.person_id, persons.c.display_name))
        }
        person_index: dict[tuple[str, str], set[str]] = {}

        resolved_identities = connection.execute(
            sa.select(
                identities.c.person_id,
                identities.c.source,
                identities.c.kind,
                identities.c.value,
            ).where(identities.c.person_id.is_not(None))
        ).all()
        for row in resolved_identities:
            bucket = _kind_bucket(row.source, row.kind, row.value)
            if bucket is not None:
                person_index.setdefault(bucket, set()).add(row.person_id)

        resolved_alias_rows = connection.execute(
            sa.select(
                identities.c.person_id,
                identities.c.source.label("identity_source"),
                identities.c.kind,
                identity_aliases.c.value,
                identity_aliases.c.source,
            )
            .join(identities, identities.c.identity_id == identity_aliases.c.identity_id)
            .where(identities.c.person_id.is_not(None))
        ).all()
        for row in resolved_alias_rows:
            bucket = _kind_bucket(row.identity_source, row.kind, row.value, row.source)
            if bucket is not None:
                person_index.setdefault(bucket, set()).add(row.person_id)

        unresolved_rows = connection.execute(
            sa.select(
                identities.c.identity_id,
                identities.c.source,
                identities.c.kind,
                identities.c.value,
                identities.c.created_at,
            )
            .where(identities.c.person_id.is_(None))
            .order_by(identities.c.source, identities.c.kind, identities.c.value)
        ).all()
        alias_rows = connection.execute(
            sa.select(
                identity_aliases.c.identity_id,
                identity_aliases.c.value,
                identity_aliases.c.source,
            ).order_by(
                identity_aliases.c.identity_id, identity_aliases.c.value, identity_aliases.c.source
            )
        ).all()

    aliases_by_identity: dict[str, list[tuple[str, str]]] = {}
    for row in alias_rows:
        aliases_by_identity.setdefault(row.identity_id, []).append((row.value, row.source))

    records: list[UnresolvedIdentityRecord] = []
    for row in unresolved_rows:
        evidence_by_person: dict[str, list[str]] = {}
        seen_evidence: set[tuple[str, str]] = set()
        bucket = _kind_bucket(row.source, row.kind, row.value)
        if bucket is not None:
            for person_id in sorted(person_index.get(bucket, set())):
                evidence = f"Matched {row.kind} value {row.value}"
                key = (person_id, evidence)
                if key not in seen_evidence:
                    evidence_by_person.setdefault(person_id, []).append(evidence)
                    seen_evidence.add(key)

        identity_aliases_rows = aliases_by_identity.get(row.identity_id, [])
        for alias_value, alias_source in identity_aliases_rows:
            alias_bucket = _kind_bucket(row.source, row.kind, alias_value, alias_source)
            if alias_bucket is None:
                continue
            for person_id in sorted(person_index.get(alias_bucket, set())):
                evidence = f"Matched alias {alias_value} via {alias_source}"
                key = (person_id, evidence)
                if key not in seen_evidence:
                    evidence_by_person.setdefault(person_id, []).append(evidence)
                    seen_evidence.add(key)

        suggested_matches = [
            SuggestedPersonMatch(
                person_id=person_id,
                display_name=person_lookup.get(person_id, person_id),
                confidence="medium" if len(evidence) > 1 else "low",
                evidence=evidence,
            )
            for person_id, evidence in sorted(
                evidence_by_person.items(),
                key=lambda item: (-len(item[1]), person_lookup.get(item[0], item[0]), item[0]),
            )
        ]
        if suggested_only and not suggested_matches:
            continue
        records.append(
            UnresolvedIdentityRecord(
                identity_id=row.identity_id,
                source=row.source,
                kind=row.kind,
                value=row.value,
                created_at=row.created_at,
                aliases=identity_aliases_rows,
                suggested_matches=suggested_matches,
            )
        )

    return records
