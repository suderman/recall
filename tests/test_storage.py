from __future__ import annotations

import sqlite3

from recall.storage.db import initialize_database
from recall.storage.paths import RecallPaths


def test_initialize_database_creates_core_tables(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    initialize_database(paths)

    with sqlite3.connect(paths.database) as connection:
        rows = connection.execute("select name from sqlite_master where type = 'table'").fetchall()

    table_names = {name for (name,) in rows}
    assert {"persons", "identities", "aliases", "resolutions", "connector_cursors"} <= table_names
