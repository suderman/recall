from __future__ import annotations

from pathlib import Path

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


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def create_engine(paths: RecallPaths) -> Engine:
    paths.ensure_directories()
    return sa.create_engine(sqlite_url(paths.database), future=True)


def ensure_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    inspector = sa.inspect(engine)
    if "resolutions" not in inspector.get_table_names():
        return
    resolution_columns = {column["name"] for column in inspector.get_columns("resolutions")}
    with engine.begin() as connection:
        if "valid_from" not in resolution_columns:
            connection.execute(sa.text("ALTER TABLE resolutions ADD COLUMN valid_from TEXT"))
        if "valid_to" not in resolution_columns:
            connection.execute(sa.text("ALTER TABLE resolutions ADD COLUMN valid_to TEXT"))


def connect(paths: RecallPaths) -> Connection:
    engine = create_engine(paths)
    ensure_schema(engine)
    return engine.connect()


def initialize_database(paths: RecallPaths) -> Engine:
    paths.ensure_directories()
    engine = create_engine(paths)
    ensure_schema(engine)
    return engine
