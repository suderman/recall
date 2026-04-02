from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from recall.connectors.telegram.entities import sync_telegram_entities
from recall.connectors.slack.entities import sync_slack_entities
from recall.entities.resolve import match_entities
from recall.entities.storage import upsert_identities, upsert_persons
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telegram"


def _copy_telegram_fixture_capture(tmp_path: Path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "updates.jsonl", target_dir / "updates.jsonl")
    return paths


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
            "select person_id from identities where identity_id = 'ident_bluebubbles_plus15551234567'"
        ).fetchone()
        resolution = connection.execute(
            "select method, confidence from resolutions where identity_id = 'ident_bluebubbles_plus15551234567'"
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
            "select person_id from identities where identity_id = 'ident_slack_email_ariel_at_example_com'"
        ).fetchone()

    assert slack_user == ("person_telegram_user_42",)
    assert slack_email == ("person_telegram_user_42",)
