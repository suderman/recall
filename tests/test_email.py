from __future__ import annotations

import sqlite3
from pathlib import Path

from recall.connectors.email.entities import sync_email_entities
from recall.connectors.email.normalize import normalize_email_day
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "email" / "messages"


def _fixture_runner():
    fixture_paths = sorted(str(path) for path in FIXTURE_DIR.glob("*.eml"))

    def run(arguments: list[str]) -> str:
        assert arguments[:4] == ["notmuch", "search", "--output=files", "--format=text"]
        return "\n".join(fixture_paths) + "\n"

    return run


def test_normalize_email_day_tags_conversations_and_noise(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    normalized_path = normalize_email_day(
        paths,
        date="2026-03-31",
        runner=_fixture_runner(),
    )

    records = read_jsonl(normalized_path)
    assert len(records) == 4

    by_subject = {record["conversation_label"]: record for record in records}
    conversation = by_subject["Project check-in"]
    assert conversation["kind"] == "email"
    assert conversation["sender_identity_id"] == "ident_email_ariel_example_com"
    assert conversation["participant_identity_ids"] == [
        "ident_email_ariel_example_com",
        "ident_email_jon_example_com",
    ]
    assert "conversation_candidate" in conversation["tags"]
    assert conversation["source_urls"] == ["https://example.com/thread"]

    marketing = by_subject["7 tips for inbox zero"]
    assert "bulk" in marketing["tags"]
    assert "marketing" in marketing["tags"]
    assert "non_conversational" in marketing["tags"]

    transactional = by_subject["Your receipt"]
    assert transactional["text"] == "Your payment posted successfully."
    assert "transactional" in transactional["tags"]

    malformed = by_subject["Re: Project check-in"]
    assert malformed["timestamp"] == "2026-03-31T00:00:00Z"
    assert "timestamp_fallback" in malformed["tags"]
    assert malformed["thread_id"] == "msg-conversation@example.com"


def test_sync_email_entities_skips_bulk_and_transactional_senders(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    result = sync_email_entities(paths, date="2026-03-31", runner=_fixture_runner())

    assert result.identities_synced == 3
    assert result.aliases_synced == 3

    with sqlite3.connect(paths.database) as connection:
        identities = connection.execute(
            "select identity_id, value from identities order by identity_id"
        ).fetchall()
        aliases = connection.execute(
            "select identity_id, value, source from identity_aliases order by identity_id, value"
        ).fetchall()

    assert identities == [
        ("ident_email_ariel_example_com", "ariel@example.com"),
        ("ident_email_chris_example_com", "chris@example.com"),
        ("ident_email_jon_example_com", "jon@example.com"),
    ]
    assert aliases == [
        ("ident_email_ariel_example_com", "Ariel Example", "email_header_display_name"),
        ("ident_email_chris_example_com", "Chris Human", "email_header_display_name"),
        ("ident_email_jon_example_com", "Jon Recipient", "email_header_display_name"),
    ]


def test_sync_email_entities_is_idempotent(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)

    first = sync_email_entities(paths, date="2026-03-31", runner=_fixture_runner())
    second = sync_email_entities(paths, date="2026-03-31", runner=_fixture_runner())

    assert first.identities_synced == second.identities_synced == 3
    assert first.aliases_synced == second.aliases_synced == 3

    with sqlite3.connect(paths.database) as connection:
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]

    assert identity_count == 3
    assert alias_count == 3
