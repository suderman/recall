from __future__ import annotations
from dataclasses import dataclass

import sqlalchemy as sa

from recall.entities.config import load_entity_resolution_config
from recall.entities.storage import upsert_resolutions
from recall.storage.db import (
    aliases,
    create_engine,
    identities,
    identity_aliases,
    metadata,
    persons,
    resolutions,
)
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class EntityMatchResult:
    manual_resolutions_applied: int
    automatic_resolutions_applied: int
    person_merges_applied: int


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


def _resolution_row(
    identity_id: str,
    person_id: str,
    *,
    confidence: str,
    method: str,
    evidence: list[str],
    created_at: str,
) -> dict[str, object]:
    resolution_id = f"res_{identity_id}_{person_id}_{method}".replace("@", "at_")
    return {
        "resolution_id": resolution_id,
        "identity_id": identity_id,
        "person_id": person_id,
        "confidence": confidence,
        "method": method,
        "evidence": evidence,
        "created_at": created_at,
    }


def _bind_identity_to_person(connection: sa.Connection, identity_id: str, person_id: str) -> None:
    connection.execute(
        sa.update(identities)
        .where(identities.c.identity_id == identity_id)
        .values(person_id=person_id)
    )


def _apply_person_merge(connection: sa.Connection, from_person_id: str, to_person_id: str) -> bool:
    if from_person_id == to_person_id:
        return False
    exists = connection.execute(
        sa.select(persons.c.person_id).where(persons.c.person_id == from_person_id)
    ).first()
    target = connection.execute(
        sa.select(persons.c.person_id).where(persons.c.person_id == to_person_id)
    ).first()
    if exists is None or target is None:
        return False

    connection.execute(
        sa.update(identities)
        .where(identities.c.person_id == from_person_id)
        .values(person_id=to_person_id)
    )
    connection.execute(
        sa.update(aliases)
        .where(aliases.c.person_id == from_person_id)
        .values(person_id=to_person_id)
    )
    connection.execute(
        sa.update(resolutions)
        .where(resolutions.c.person_id == from_person_id)
        .values(person_id=to_person_id)
    )
    connection.execute(sa.delete(persons).where(persons.c.person_id == from_person_id))
    return True


def match_entities(paths: RecallPaths) -> EntityMatchResult:
    paths.ensure_directories()
    config = load_entity_resolution_config(paths)
    manual_rows: list[dict[str, object]] = []
    automatic_rows: list[dict[str, object]] = []

    engine = create_engine(paths)
    metadata.create_all(engine)

    with engine.begin() as connection:
        unresolved = connection.execute(
            sa.select(
                identities.c.identity_id,
                identities.c.source,
                identities.c.kind,
                identities.c.value,
                identities.c.person_id,
                identities.c.created_at,
            ).order_by(identities.c.identity_id)
        ).all()

        manually_bound_identity_ids: set[str] = set()

        for rule in config.identity_resolutions:
            row = connection.execute(
                sa.select(identities.c.identity_id, identities.c.created_at)
                .where(identities.c.source == rule.source)
                .where(identities.c.kind == rule.kind)
                .where(identities.c.value == rule.value)
            ).first()
            if row is None:
                continue
            _bind_identity_to_person(connection, row.identity_id, rule.person_id)
            manually_bound_identity_ids.add(row.identity_id)
            manual_rows.append(
                _resolution_row(
                    row.identity_id,
                    rule.person_id,
                    confidence=rule.confidence,
                    method=rule.method,
                    evidence=rule.evidence or ["Applied manual identity resolution rule"],
                    created_at=row.created_at,
                )
            )

        person_index: dict[tuple[str, str], set[str]] = {}
        resolved_identities = connection.execute(
            sa.select(
                identities.c.identity_id,
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

        resolved_aliases = connection.execute(
            sa.select(
                identity_aliases.c.identity_id,
                identity_aliases.c.value,
                identity_aliases.c.source,
                identities.c.person_id,
                identities.c.kind,
                identities.c.source.label("identity_source"),
            )
            .join(identities, identities.c.identity_id == identity_aliases.c.identity_id)
            .where(identities.c.person_id.is_not(None))
        ).all()
        for row in resolved_aliases:
            bucket = _kind_bucket(row.identity_source, row.kind, row.value, row.source)
            if bucket is not None:
                person_index.setdefault(bucket, set()).add(row.person_id)

        for row in unresolved:
            if row.person_id is not None:
                continue
            if row.identity_id in manually_bound_identity_ids:
                continue
            bucket = _kind_bucket(row.source, row.kind, row.value)
            matched_person_ids: set[str] = set()
            if bucket is not None:
                matched_person_ids.update(person_index.get(bucket, set()))

            alias_rows = connection.execute(
                sa.select(identity_aliases.c.value, identity_aliases.c.source).where(
                    identity_aliases.c.identity_id == row.identity_id
                )
            ).all()
            for alias_row in alias_rows:
                alias_bucket = _kind_bucket(row.source, row.kind, alias_row.value, alias_row.source)
                if alias_bucket is not None:
                    matched_person_ids.update(person_index.get(alias_bucket, set()))

            if len(matched_person_ids) != 1:
                continue
            person_id = next(iter(matched_person_ids))
            _bind_identity_to_person(connection, row.identity_id, person_id)
            automatic_rows.append(
                _resolution_row(
                    row.identity_id,
                    person_id,
                    confidence="high",
                    method="cross_source_exact_match",
                    evidence=[f"Matched {row.kind} value across sources"],
                    created_at=row.created_at,
                )
            )

        merges_applied = 0
        for merge in config.person_merges:
            if _apply_person_merge(connection, merge.from_person_id, merge.to_person_id):
                merges_applied += 1

    manual_count = upsert_resolutions(paths, manual_rows)
    automatic_count = upsert_resolutions(paths, automatic_rows)
    return EntityMatchResult(
        manual_resolutions_applied=manual_count,
        automatic_resolutions_applied=automatic_count,
        person_merges_applied=merges_applied,
    )
