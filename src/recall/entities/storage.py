from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from recall.storage.db import (
    aliases,
    create_engine,
    ensure_schema,
    identities,
    identity_aliases,
    persons,
    resolutions,
)
from recall.storage.paths import RecallPaths


def upsert_persons(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.begin() as connection:
        for row in rows:
            payload = dict(row)
            tags = payload.pop("tags", None)
            if tags is not None:
                payload["tags_json"] = json.dumps(tags)
            statement = sqlite_insert(persons).values(**payload)
            statement = statement.on_conflict_do_update(
                index_elements=[persons.c.person_id],
                set_={
                    "display_name": statement.excluded.display_name,
                    "sort_name": statement.excluded.sort_name,
                    "notes": statement.excluded.notes,
                    "tags_json": statement.excluded.tags_json,
                },
            )
            connection.execute(statement)

    return len(rows)


def upsert_identities(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.begin() as connection:
        for row in rows:
            statement = sqlite_insert(identities).values(**row)
            statement = statement.on_conflict_do_update(
                index_elements=[identities.c.source, identities.c.kind, identities.c.value],
                set_={
                    "person_id": sa.func.coalesce(
                        statement.excluded.person_id,
                        identities.c.person_id,
                    ),
                    "label": statement.excluded.label,
                    "is_primary": statement.excluded.is_primary,
                    "status": statement.excluded.status,
                    "valid_from": statement.excluded.valid_from,
                    "valid_to": statement.excluded.valid_to,
                },
            )
            connection.execute(statement)

    return len(rows)


def upsert_identity_aliases(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.begin() as connection:
        for row in rows:
            statement = sqlite_insert(identity_aliases).values(**row)
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    identity_aliases.c.identity_id,
                    identity_aliases.c.value,
                    identity_aliases.c.source,
                ]
            )
            connection.execute(statement)

    return len(rows)


def upsert_aliases(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.begin() as connection:
        for row in rows:
            statement = sqlite_insert(aliases).values(**row)
            statement = statement.on_conflict_do_nothing(index_elements=[aliases.c.alias_id])
            connection.execute(statement)

    return len(rows)


def upsert_resolutions(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.begin() as connection:
        for row in rows:
            payload = dict(row)
            evidence = payload.pop("evidence", None)
            if evidence is not None:
                payload["evidence_json"] = json.dumps(evidence)
            statement = sqlite_insert(resolutions).values(**payload)
            statement = statement.on_conflict_do_nothing(
                index_elements=[resolutions.c.resolution_id]
            )
            connection.execute(statement)

    return len(rows)
