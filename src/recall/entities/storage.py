from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from recall.storage.db import create_engine, identities, identity_aliases, metadata
from recall.storage.paths import RecallPaths


def upsert_identities(paths: RecallPaths, records: Iterable[dict[str, Any]]) -> int:
    rows = list(records)
    if not rows:
        return 0

    engine = create_engine(paths)
    metadata.create_all(engine)

    with engine.begin() as connection:
        for row in rows:
            statement = sqlite_insert(identities).values(**row)
            statement = statement.on_conflict_do_update(
                index_elements=[identities.c.source, identities.c.kind, identities.c.value],
                set_={
                    "label": statement.excluded.label,
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
    metadata.create_all(engine)

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
