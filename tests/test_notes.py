from __future__ import annotations

import pytest

import recall.normalize.rebuild as replay
from recall.connectors.notes import normalize_notes_day, note_path
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-10-08"
TZ = "America/Edmonton"


def _workspace(tmp_path, text=None):
    paths = RecallPaths.from_root(tmp_path / "archive")
    paths.sources_config.mkdir(parents=True)
    notes = tmp_path / "notes"
    (paths.sources_config / "notes.toml").write_text(f'directory = "{notes}"\n')
    if text is not None:
        path = note_path(notes, DAY)
        path.parent.mkdir(parents=True)
        path.write_text(text)
    return paths


def test_each_paragraph_becomes_a_cited_author_note(tmp_path):
    paths = _workspace(
        tmp_path,
        "#+TITLE: Notes\n* Thursday\n\nWent to Davy's karate after work.\nHe got his gear.\n\n"
        "Ness and I had dinner at Sushi Hiro.\n",
    )

    normalize_notes_day(paths, date=DAY, timezone_name=TZ)

    rows = read_jsonl(paths.normalized_event_path(DAY))
    assert [row["text"] for row in rows] == [
        "Went to Davy's karate after work.\nHe got his gear.",
        "Ness and I had dinner at Sushi Hiro.",
    ]
    assert {row["source"] for row in rows} == {"notes"}
    assert {row["kind"] for row in rows} == {"author_note"}
    assert rows[0]["raw_ref"]["path"] == str(note_path(tmp_path / "notes", DAY))
    assert rows[0]["raw_ref"]["locator"]["paragraph"] == 1
    assert all(row["date"] == DAY for row in rows)


def test_note_ids_are_stable_and_follow_the_text(tmp_path):
    paths = _workspace(tmp_path, "First note.\n\nSecond note.\n")
    normalize_notes_day(paths, date=DAY, timezone_name=TZ)
    first = [row["event_id"] for row in read_jsonl(paths.normalized_event_path(DAY))]
    normalize_notes_day(paths, date=DAY, timezone_name=TZ)
    assert [row["event_id"] for row in read_jsonl(paths.normalized_event_path(DAY))] == first


def test_missing_note_file_is_an_empty_day_and_missing_config_fails(tmp_path):
    paths = _workspace(tmp_path)
    normalize_notes_day(paths, date=DAY, timezone_name=TZ)
    assert not paths.normalized_event_path(DAY).exists()
    (paths.sources_config / "notes.toml").unlink()
    with pytest.raises(ValueError, match="notes.toml"):
        normalize_notes_day(paths, date=DAY, timezone_name=TZ)


def test_replay_queries_notes_and_drops_removed_paragraphs(tmp_path):
    paths = _workspace(tmp_path, "Kept note.\n\nLater removed.\n")
    output = RecallPaths.from_root(tmp_path / "replay")

    def run():
        return replay.rebuild_range(
            paths, output, first=DAY, last=DAY, sources=["notes"], timezone_name=TZ
        )

    assert [job["status"] for job in run()] == ["success"]
    note_path(tmp_path / "notes", DAY).write_text("Kept note.\n")
    run()

    rows = read_jsonl(output.normalized_event_path(DAY))
    assert [row["text"] for row in rows] == ["Kept note."]
