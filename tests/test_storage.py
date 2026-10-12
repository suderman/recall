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
    assert {
        "persons",
        "identities",
        "aliases",
        "identity_aliases",
        "resolutions",
        "connector_cursors",
    } <= table_names


def test_busy_lock_names_the_lock_and_keeps_blocking_io_error(tmp_path) -> None:
    import fcntl

    import pytest

    from recall.storage.locks import lock_nonblocking

    path = tmp_path / "writer.lock"
    with path.open("a") as held, path.open("a") as waiting:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(BlockingIOError, match="writer.lock is held by another Recall process"):
            lock_nonblocking(waiting, shared=True)
