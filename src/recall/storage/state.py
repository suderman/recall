from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from recall.storage.db import connector_cursors, create_engine, ensure_schema
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class ConnectorCursor:
    source: str
    account: str
    cursor_key: str
    cursor_value: str
    updated_at: str


def _cursor_row_to_model(row) -> ConnectorCursor:
    return ConnectorCursor(
        source=row.source,
        account=row.account,
        cursor_key=row.cursor_key,
        cursor_value=row.cursor_value,
        updated_at=row.updated_at,
    )


def get_connector_cursor(
    paths: RecallPaths,
    *,
    source: str,
    account: str,
    cursor_key: str,
) -> ConnectorCursor | None:
    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.connect() as connection:
        row = connection.execute(
            select(connector_cursors)
            .where(connector_cursors.c.source == source)
            .where(connector_cursors.c.account == account)
            .where(connector_cursors.c.cursor_key == cursor_key)
        ).fetchone()

    if row is None:
        return None

    return _cursor_row_to_model(row)


def list_connector_cursors(
    paths: RecallPaths,
    *,
    source: str,
    account: str,
) -> list[ConnectorCursor]:
    engine = create_engine(paths)
    ensure_schema(engine)

    with engine.connect() as connection:
        rows = connection.execute(
            select(connector_cursors)
            .where(connector_cursors.c.source == source)
            .where(connector_cursors.c.account == account)
            .order_by(connector_cursors.c.cursor_key)
        ).fetchall()

    return [_cursor_row_to_model(row) for row in rows]


def set_connector_cursor(
    paths: RecallPaths,
    *,
    source: str,
    account: str,
    cursor_key: str,
    cursor_value: str,
    updated_at: str | None = None,
) -> ConnectorCursor:
    engine = create_engine(paths)
    ensure_schema(engine)
    timestamp = updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with engine.begin() as connection:
        statement = sqlite_insert(connector_cursors).values(
            source=source,
            account=account,
            cursor_key=cursor_key,
            cursor_value=cursor_value,
            updated_at=timestamp,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                connector_cursors.c.source,
                connector_cursors.c.account,
                connector_cursors.c.cursor_key,
            ],
            set_={
                "cursor_value": statement.excluded.cursor_value,
                "updated_at": statement.excluded.updated_at,
            },
        )
        connection.execute(statement)

    return ConnectorCursor(
        source=source,
        account=account,
        cursor_key=cursor_key,
        cursor_value=cursor_value,
        updated_at=timestamp,
    )
