from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from recall.connectors.email.entities import sync_email_entities
from recall.connectors.slack.entities import sync_slack_entities
from recall.connectors.telegram.entities import sync_telegram_entities
from recall.entities.enrich import enrich_events_with_people
from recall.entities.query import list_unresolved_identities
from recall.entities.resolve import match_entities
from recall.entities.storage import upsert_identities, upsert_persons, upsert_resolutions
from recall.normalize.events import NormalizedEvent
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telegram"
EMAIL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "email" / "messages"


def _copy_telegram_fixture_capture(tmp_path: Path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "updates.jsonl", target_dir / "updates.jsonl")
    return paths


def _email_runner(arguments: list[str]) -> str:
    assert arguments[:4] == ["notmuch", "search", "--output=files", "--format=text"]
    fixture_paths = sorted(str(path) for path in EMAIL_FIXTURE_DIR.glob("*.eml"))
    return "\n".join(fixture_paths) + "\n"


def test_match_entities_automatically_links_bluebubbles_phone_to_telegram_person(tmp_path) -> None:
    paths = _copy_telegram_fixture_capture(tmp_path)
    sync_telegram_entities(paths, date="2026-03-31")
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_bluebubbles_plus15551234567",
                "person_id": None,
                "source": "bluebubbles",
                "kind": "phone",
                "value": "+15551234567",
                "label": "BlueBubbles phone",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T18:00:00Z",
            }
        ],
    )

    result = match_entities(paths)

    assert result.manual_resolutions_applied == 0
    assert result.automatic_resolutions_applied == 1
    with sqlite3.connect(paths.database) as connection:
        identity = connection.execute(
            "select person_id from identities "
            "where identity_id = 'ident_bluebubbles_plus15551234567'"
        ).fetchone()
        resolution = connection.execute(
            "select method, confidence from resolutions "
            "where identity_id = 'ident_bluebubbles_plus15551234567'"
        ).fetchone()

    assert identity == ("person_telegram_user_42",)
    assert resolution == ("cross_source_exact_match", "high")


def test_match_entities_applies_manual_resolution_and_person_merge(tmp_path) -> None:
    paths = _copy_telegram_fixture_capture(tmp_path)
    sync_telegram_entities(paths, date="2026-03-31")
    upsert_persons(
        paths,
        [
            {
                "person_id": "person_duplicate",
                "display_name": "Ariel Duplicate",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T18:00:00Z",
            }
        ],
    )
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_slack_U123",
                "person_id": None,
                "source": "slack",
                "kind": "user_id",
                "value": "U123",
                "label": "Slack user ID",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T18:00:00Z",
            },
            {
                "identity_id": "ident_duplicate_handle",
                "person_id": "person_duplicate",
                "source": "bluebubbles",
                "kind": "handle",
                "value": "ariel-duplicate",
                "label": "BlueBubbles handle",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T18:00:00Z",
            },
        ],
    )
    manual_path = paths.entity_resolution_config / "manual.toml"
    manual_path.write_text(
        """
[[identity_resolution]]
source = "slack"
kind = "user_id"
value = "U123"
person_id = "person_telegram_user_42"
confidence = "high"
method = "manual_override"
evidence = ["Reviewed manually"]

[[person_merge]]
from_person_id = "person_duplicate"
to_person_id = "person_telegram_user_42"
reason = "Duplicate person"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = match_entities(paths)

    assert result.manual_resolutions_applied == 1
    assert result.person_merges_applied == 1
    with sqlite3.connect(paths.database) as connection:
        slack_identity = connection.execute(
            "select person_id from identities where identity_id = 'ident_slack_U123'"
        ).fetchone()
        merged_identity = connection.execute(
            "select person_id from identities where identity_id = 'ident_duplicate_handle'"
        ).fetchone()
        duplicate_person = connection.execute(
            "select count(*) from persons where person_id = 'person_duplicate'"
        ).fetchone()

    assert slack_identity == ("person_telegram_user_42",)
    assert merged_identity == ("person_telegram_user_42",)
    assert duplicate_person == (0,)


def test_match_entities_links_slack_user_through_profile_email(tmp_path) -> None:
    paths = _copy_telegram_fixture_capture(tmp_path)
    slack_dir = paths.raw_capture_dir("slack", "2026-03-31")
    slack_dir.mkdir(parents=True, exist_ok=True)
    for name in ("metadata.json", "conversations.json", "messages.jsonl"):
        shutil.copy(Path(__file__).parent / "fixtures" / "slack_capture" / name, slack_dir / name)

    sync_telegram_entities(paths, date="2026-03-31")
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_email_ariel_example_com",
                "person_id": "person_telegram_user_42",
                "source": "email",
                "kind": "email",
                "value": "ariel@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T18:00:00Z",
            }
        ],
    )
    sync_slack_entities(paths, date="2026-03-31")

    result = match_entities(paths)

    assert result.automatic_resolutions_applied >= 2
    with sqlite3.connect(paths.database) as connection:
        slack_user = connection.execute(
            "select person_id from identities where identity_id = 'ident_slack_UPEER'"
        ).fetchone()
        slack_email = connection.execute(
            "select person_id from identities "
            "where identity_id = 'ident_slack_email_ariel_at_example_com'"
        ).fetchone()

    assert slack_user == ("person_telegram_user_42",)
    assert slack_email == ("person_telegram_user_42",)


def test_match_entities_links_slack_user_through_synced_email_identity(tmp_path) -> None:
    paths = _copy_telegram_fixture_capture(tmp_path)
    slack_dir = paths.raw_capture_dir("slack", "2026-03-31")
    slack_dir.mkdir(parents=True, exist_ok=True)
    for name in ("metadata.json", "conversations.json", "messages.jsonl"):
        shutil.copy(Path(__file__).parent / "fixtures" / "slack_capture" / name, slack_dir / name)

    sync_telegram_entities(paths, date="2026-03-31")
    sync_email_entities(paths, date="2026-03-31", runner=_email_runner)
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_email_ariel_example_com",
                "person_id": "person_telegram_user_42",
                "source": "email",
                "kind": "email",
                "value": "ariel@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T17:31:07Z",
            }
        ],
    )
    sync_slack_entities(paths, date="2026-03-31")

    result = match_entities(paths)

    assert result.automatic_resolutions_applied >= 2
    with sqlite3.connect(paths.database) as connection:
        slack_user = connection.execute(
            "select person_id from identities where identity_id = 'ident_slack_UPEER'"
        ).fetchone()
        email_identity = connection.execute(
            "select person_id from identities where identity_id = 'ident_email_ariel_example_com'"
        ).fetchone()

    assert slack_user == ("person_telegram_user_42",)
    assert email_identity == ("person_telegram_user_42",)


def test_match_entities_preserves_dated_manual_resolution_windows(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    upsert_persons(
        paths,
        [
            {
                "person_id": "person_old_owner",
                "display_name": "Old Owner",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "person_id": "person_new_owner",
                "display_name": "New Owner",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
        ],
    )
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_email_reused_example_com",
                "person_id": None,
                "source": "email",
                "kind": "email",
                "value": "reused@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            }
        ],
    )
    manual_path = paths.entity_resolution_config / "manual.toml"
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_path.write_text(
        """
[[identity_resolution]]
source = "email"
kind = "email"
value = "reused@example.com"
person_id = "person_old_owner"
valid_to = "2026-03-31"

[[identity_resolution]]
source = "email"
kind = "email"
value = "reused@example.com"
person_id = "person_new_owner"
valid_from = "2026-04-01"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = match_entities(paths)

    assert result.manual_resolutions_applied == 2
    with sqlite3.connect(paths.database) as connection:
        resolutions = connection.execute(
            "select person_id, valid_from, valid_to from resolutions "
            "where identity_id = 'ident_email_reused_example_com' order by person_id"
        ).fetchall()
        identity = connection.execute(
            "select person_id from identities where identity_id = 'ident_email_reused_example_com'"
        ).fetchone()

    assert resolutions == [
        ("person_new_owner", "2026-04-01", None),
        ("person_old_owner", None, "2026-03-31"),
    ]
    assert identity == (None,)


def test_list_unresolved_identities_includes_ambiguous_suggested_matches(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    upsert_persons(
        paths,
        [
            {
                "person_id": "person_one",
                "display_name": "Ariel One",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "person_id": "person_two",
                "display_name": "Ariel Two",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
        ],
    )
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_slack_shared",
                "person_id": "person_one",
                "source": "slack",
                "kind": "email",
                "value": "shared@example.com",
                "label": "Slack profile email",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "identity_id": "ident_asana_shared",
                "person_id": "person_two",
                "source": "asana",
                "kind": "email",
                "value": "shared@example.com",
                "label": "Asana email",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "identity_id": "ident_email_shared_example_com",
                "person_id": None,
                "source": "email",
                "kind": "email",
                "value": "shared@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "identity_id": "ident_orphan_handle",
                "person_id": None,
                "source": "telegram",
                "kind": "handle",
                "value": "orphan-handle",
                "label": "Telegram handle",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
        ],
    )

    rows = list_unresolved_identities(paths)

    assert [row.identity_id for row in rows] == [
        "ident_email_shared_example_com",
        "ident_orphan_handle",
    ]
    suggested = rows[0].suggested_matches
    assert [match.person_id for match in suggested] == ["person_one", "person_two"]
    assert all(match.confidence == "low" for match in suggested)
    assert rows[1].suggested_matches == []


def test_list_unresolved_identities_suggested_only_filters_empty_candidates(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    upsert_persons(
        paths,
        [
            {
                "person_id": "person_one",
                "display_name": "Ariel One",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            }
        ],
    )
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_slack_shared",
                "person_id": "person_one",
                "source": "slack",
                "kind": "email",
                "value": "shared@example.com",
                "label": "Slack profile email",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "identity_id": "ident_email_shared_example_com",
                "person_id": None,
                "source": "email",
                "kind": "email",
                "value": "shared@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "identity_id": "ident_orphan_handle",
                "person_id": None,
                "source": "telegram",
                "kind": "handle",
                "value": "orphan-handle",
                "label": "Telegram handle",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            },
        ],
    )

    rows = list_unresolved_identities(paths, suggested_only=True)

    assert [row.identity_id for row in rows] == ["ident_email_shared_example_com"]


def test_enrich_events_with_people_uses_event_timestamp_against_resolution_windows(
    tmp_path,
) -> None:
    paths = RecallPaths.from_root(tmp_path)
    upsert_persons(
        paths,
        [
            {
                "person_id": "person_old_owner",
                "display_name": "Old Owner",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "person_id": "person_new_owner",
                "display_name": "New Owner",
                "sort_name": None,
                "notes": "",
                "tags": [],
                "created_at": "2026-03-31T00:00:00Z",
            },
        ],
    )
    upsert_identities(
        paths,
        [
            {
                "identity_id": "ident_email_reused_example_com",
                "person_id": None,
                "source": "email",
                "kind": "email",
                "value": "reused@example.com",
                "label": "Email address",
                "is_primary": False,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-03-31T00:00:00Z",
            }
        ],
    )
    upsert_resolutions(
        paths,
        [
            {
                "resolution_id": "res_old",
                "identity_id": "ident_email_reused_example_com",
                "person_id": "person_old_owner",
                "confidence": "high",
                "method": "manual_override",
                "valid_from": None,
                "valid_to": "2026-03-31",
                "evidence": ["before handoff"],
                "created_at": "2026-03-31T00:00:00Z",
            },
            {
                "resolution_id": "res_new",
                "identity_id": "ident_email_reused_example_com",
                "person_id": "person_new_owner",
                "confidence": "high",
                "method": "manual_override",
                "valid_from": "2026-04-01",
                "valid_to": None,
                "evidence": ["after handoff"],
                "created_at": "2026-04-01T00:00:00Z",
            },
        ],
    )

    events = [
        NormalizedEvent(
            event_id="evt_old",
            source="email",
            timestamp="2026-03-31T12:00:00Z",
            date="2026-03-31",
            kind="email",
            sender_identity_id="ident_email_reused_example_com",
            participant_identity_ids=["ident_email_reused_example_com"],
        ),
        NormalizedEvent(
            event_id="evt_new",
            source="email",
            timestamp="2026-04-02T12:00:00Z",
            date="2026-04-02",
            kind="email",
            sender_identity_id="ident_email_reused_example_com",
            participant_identity_ids=["ident_email_reused_example_com"],
        ),
    ]

    enrich_events_with_people(paths, events)

    assert events[0].sender_person_id == "person_old_owner"
    assert events[0].participant_person_ids == ["person_old_owner"]
    assert events[1].sender_person_id == "person_new_owner"
    assert events[1].participant_person_ids == ["person_new_owner"]
