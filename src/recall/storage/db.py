from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from recall.storage.paths import RecallPaths

metadata = sa.MetaData()

persons = sa.Table(
    "persons",
    metadata,
    sa.Column("person_id", sa.Text, primary_key=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("sort_name", sa.Text),
    sa.Column("notes", sa.Text, nullable=False, server_default=""),
    sa.Column("tags_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("created_at", sa.Text, nullable=False),
)

identities = sa.Table(
    "identities",
    metadata,
    sa.Column("identity_id", sa.Text, primary_key=True),
    sa.Column("person_id", sa.Text, sa.ForeignKey("persons.person_id")),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("label", sa.Text),
    sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("valid_from", sa.Text),
    sa.Column("valid_to", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.UniqueConstraint("source", "kind", "value", name="uq_identity_source_kind_value"),
)

aliases = sa.Table(
    "aliases",
    metadata,
    sa.Column("alias_id", sa.Text, primary_key=True),
    sa.Column("person_id", sa.Text, sa.ForeignKey("persons.person_id"), nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

identity_aliases = sa.Table(
    "identity_aliases",
    metadata,
    sa.Column("identity_alias_id", sa.Text, primary_key=True),
    sa.Column("identity_id", sa.Text, sa.ForeignKey("identities.identity_id"), nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.UniqueConstraint(
        "identity_id",
        "value",
        "source",
        name="uq_identity_alias_identity_value_source",
    ),
)

resolutions = sa.Table(
    "resolutions",
    metadata,
    sa.Column("resolution_id", sa.Text, primary_key=True),
    sa.Column("identity_id", sa.Text, sa.ForeignKey("identities.identity_id"), nullable=False),
    sa.Column("person_id", sa.Text, sa.ForeignKey("persons.person_id"), nullable=False),
    sa.Column("confidence", sa.Text, nullable=False),
    sa.Column("method", sa.Text, nullable=False),
    sa.Column("valid_from", sa.Text),
    sa.Column("valid_to", sa.Text),
    sa.Column("evidence_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("created_at", sa.Text, nullable=False),
)

connector_cursors = sa.Table(
    "connector_cursors",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("account", sa.Text, primary_key=True),
    sa.Column("cursor_key", sa.Text, primary_key=True),
    sa.Column("cursor_value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)


# Columns added after the first schema. Existing databases gain them only through migrate.
MIGRATED_COLUMNS = (("resolutions", "valid_from"), ("resolutions", "valid_to"))


class SchemaMigrationRequired(RuntimeError):
    pass


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def create_engine(paths: RecallPaths) -> Engine:
    paths.state.mkdir(parents=True, exist_ok=True)
    return sa.create_engine(sqlite_url(paths.database), future=True)


def _missing_columns(engine: Engine) -> list[str]:
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in {table for table, _ in MIGRATED_COLUMNS} & tables
    }
    return [
        f"{table}.{column}"
        for table, column in MIGRATED_COLUMNS
        if table in columns and column not in columns[table]
    ]


def ensure_schema(engine: Engine) -> None:
    """Create absent tables. Never alter existing ones; that is an explicit migration."""
    missing = _missing_columns(engine)
    if missing:
        raise SchemaMigrationRequired(
            f"{engine.url.database} lacks {', '.join(missing)}; "
            "back it up with `recall migrate --root ROOT` first"
        )
    metadata.create_all(engine)


def migrate_database(paths: RecallPaths) -> dict[str, Any]:
    """Back up an existing database, then add any missing migrated columns."""
    if not paths.database.is_file():
        raise ValueError(f"No Recall database at {paths.database}")
    engine = create_engine(paths)
    try:
        missing = _missing_columns(engine)
        report: dict[str, Any] = {
            "database": paths.relative_to_root(paths.database),
            "added": missing,
            "backup": None,
        }
        if not missing:
            return report
        backups = paths.state / "backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = backups / f"{paths.database.name}.{stamp}"
        if backup.exists():
            raise ValueError(f"Migration backup already exists: {backup}")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with sqlite3.connect(paths.database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        with engine.begin() as connection:
            for name in missing:
                table, column = name.split(".")
                connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))
        report["backup"] = paths.relative_to_root(backup)
        return report
    finally:
        engine.dispose()


def connect(paths: RecallPaths) -> Connection:
    engine = create_engine(paths)
    ensure_schema(engine)
    return engine.connect()


def initialize_database(paths: RecallPaths) -> Engine:
    paths.ensure_directories()
    engine = create_engine(paths)
    ensure_schema(engine)
    return engine
