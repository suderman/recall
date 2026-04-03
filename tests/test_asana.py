from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from recall.connectors.asana.entities import sync_asana_entities
from recall.connectors.asana.importer import import_asana_export
from recall.connectors.asana.normalize import normalize_asana_day
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "asana"


def _copy_fixture_export(tmp_path: Path) -> Path:
    export_dir = tmp_path / "asana-export"
    export_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "export.json", export_dir / "export.json")
    return export_dir


def test_import_asana_export_writes_daily_raw_envelopes(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = _copy_fixture_export(tmp_path)

    result = import_asana_export(paths, export_path=export_dir, account="work")

    assert result.tasks_imported == 1
    assert result.stories_imported == 2
    assert result.dates_written == ["2026-03-31", "2026-04-01"]
    assert (result.import_dir / "export.json").exists()

    march_rows = read_jsonl(paths.raw_capture_dir("asana", "2026-03-31") / "events.jsonl")
    april_rows = read_jsonl(paths.raw_capture_dir("asana", "2026-04-01") / "events.jsonl")
    assert [row["event_type"] for row in march_rows] == ["task", "task_story"]
    assert [row["event_type"] for row in april_rows] == ["task_completion", "task_story"]


def test_normalize_asana_day_writes_task_and_story_events(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = _copy_fixture_export(tmp_path)
    import_asana_export(paths, export_path=export_dir, account="work")

    normalized_path = normalize_asana_day(paths, date="2026-03-31")
    records = read_jsonl(normalized_path)

    asana_records = [record for record in records if record["source"] == "asana"]
    assert len(asana_records) == 2
    by_kind = {record["kind"]: record for record in asana_records}
    task = by_kind["task"]
    assert task["conversation_id"] == "task-1"
    assert task["sender_identity_id"] == "ident_asana_user-1"
    assert task["participant_identity_ids"] == [
        "ident_asana_user-1",
        "ident_asana_user-2",
        "ident_asana_user-3",
    ]
    assert task["source_urls"] == ["https://app.asana.com/0/123/task-1"]

    story = by_kind["task_story"]
    assert "comment" in story["tags"]
    assert story["thread_id"] == "task-1"
    assert story["raw_ref"]["locator"]["story_gid"] == "story-1"


def test_sync_asana_entities_is_idempotent(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = _copy_fixture_export(tmp_path)
    import_asana_export(paths, export_path=export_dir, account="work")

    first = sync_asana_entities(paths, date="2026-03-31")
    second = sync_asana_entities(paths, date="2026-03-31")

    assert first.identities_synced == second.identities_synced == 6
    assert first.aliases_synced == second.aliases_synced == 6

    with sqlite3.connect(paths.database) as connection:
        identities = connection.execute(
            "select identity_id, kind, value from identities "
            "where source = 'asana' order by identity_id"
        ).fetchall()
        aliases = connection.execute(
            "select identity_id, value, source from identity_aliases "
            "where identity_id like 'ident_asana_%' order by identity_id, value"
        ).fetchall()

    assert identities == [
        ("ident_asana_email_ariel_example_com", "email", "ariel@example.com"),
        ("ident_asana_email_jon_example_com", "email", "jon@example.com"),
        ("ident_asana_email_ops_example_com", "email", "ops@example.com"),
        ("ident_asana_user-1", "user_id", "user-1"),
        ("ident_asana_user-2", "user_id", "user-2"),
        ("ident_asana_user-3", "user_id", "user-3"),
    ]
    assert aliases == [
        ("ident_asana_user-1", "Jon Suderman", "asana_user_name"),
        ("ident_asana_user-1", "jon@example.com", "asana_user_email"),
        ("ident_asana_user-2", "Ariel Example", "asana_user_name"),
        ("ident_asana_user-2", "ariel@example.com", "asana_user_email"),
        ("ident_asana_user-3", "Ops Bot", "asana_user_name"),
        ("ident_asana_user-3", "ops@example.com", "asana_user_email"),
    ]


def test_normalize_asana_day_is_replayable(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    export_dir = _copy_fixture_export(tmp_path)
    import_asana_export(paths, export_path=export_dir, account="work")

    first_path = normalize_asana_day(paths, date="2026-03-31")
    first_output = first_path.read_text(encoding="utf-8")
    second_path = normalize_asana_day(paths, date="2026-03-31")
    second_output = second_path.read_text(encoding="utf-8")

    assert first_path == second_path
    assert first_output == second_output
