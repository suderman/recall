import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest
from test_journals import DAY, TZ, runner_config, runner_env, workspace
from test_slack_backfill import History
from typer.testing import CliRunner

from recall.cli.main import app
from recall.entities.storage import upsert_identities, upsert_persons, upsert_resolutions
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize import generate, journal, run

AUTHOR = "Example"
PREFERENCE = """[[person]]
author = "Example"
person_id = "person_partner"
relationship = "spouse"
preferred_names = ["Caz", "my spouse"]
evidence = ["Author explicitly confirmed identity, relationship and personal names"]
valid_from = "2026-01-01"
valid_to = "2026-12-31"
"""


def fixture(tmp_path):
    paths = workspace(tmp_path / "events")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    mail = next(row for row in rows if row["event_id"] == "evt_mail")
    mail.update(
        sender_identity_id="ident_self",
        participant_identity_ids=["ident_email"],
        participant_person_ids=["person_stale"],
        text="I confirmed the enrollment. C Example asked if the email was spam.",
    )
    rows.append(
        {
            **mail,
            "event_id": "evt_chat",
            "source": "telegram",
            "participant_identity_ids": ["ident_telegram"],
            "conversation_label": "Casey Example",
        }
    )
    rows.append(
        {
            **mail,
            "event_id": "evt_unknown",
            "participant_identity_ids": ["ident_unknown"],
            "text": "Another Casey Example, identity not established.",
        }
    )
    write_jsonl(paths.normalized_event_path(DAY), rows)
    entities = RecallPaths.from_root(tmp_path / "entities")
    upsert_persons(
        entities,
        [
            {
                "person_id": "person_partner",
                "display_name": "Casey Example",
                "notes": "PRIVATE_NOTES",
                "tags": ["PRIVATE_TAG"],
                "created_at": DAY,
            },
            {"person_id": "person_other", "display_name": "OTHER_PERSON", "created_at": DAY},
        ],
    )
    upsert_identities(
        entities,
        [
            {
                "identity_id": identity,
                "source": source,
                "kind": "user",
                "value": identity,
                "person_id": person,
                "created_at": DAY,
            }
            for identity, source, person in [
                ("ident_email", "email", None),
                ("ident_telegram", "telegram", "person_partner"),
                ("ident_unknown", "email", None),
            ]
        ],
    )
    upsert_resolutions(
        entities,
        [
            {
                "resolution_id": "res_email",
                "identity_id": "ident_email",
                "person_id": "person_partner",
                "confidence": "high",
                "method": "manual_override",
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "created_at": DAY,
            }
        ],
    )
    config = entities.config / "journal/people.toml"
    config.parent.mkdir(parents=True)
    config.write_text(PREFERENCE)
    return paths, entities, config


def prepare(paths, entities, author=AUTHOR):
    return journal.prepare_journal(
        paths, day=DAY, author=author, timezone_name=TZ, people_root=entities
    )


def saved_files(root):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_explicit_cross_source_context_is_read_only_and_keeps_observed_events(
    tmp_path, monkeypatch
):
    paths, entities, _ = fixture(tmp_path)
    source = paths.normalized_event_path(DAY).read_bytes()
    before = saved_files(entities.root)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("No network"))
    packet = prepare(paths, entities)
    context = json.loads((packet / "people.json").read_text())
    assert context["events"]["evt_mail"]["ident_email"] == "person_partner"
    assert context["events"]["evt_chat"]["ident_telegram"] == "person_partner"
    assert context["events"]["evt_unknown"]["ident_unknown"] is None
    assert context["events"]["evt_mail"]["ident_self"] is None
    assert context["people"] == {
        "person_partner": {
            "display_name": "Casey Example",
            "author_context": {
                "relationship": "spouse",
                "preferred_names": ["Caz", "my spouse"],
                "evidence": [
                    "Author explicitly confirmed identity, relationship and personal names"
                ],
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
            },
        }
    }
    assert paths.normalized_event_path(DAY).read_bytes() == source
    assert saved_files(entities.root) == before
    assert "PRIVATE_NOTES" not in (packet / "prompt.org").read_text()
    assert "PRIVATE_TAG" not in (packet / "people.json").read_text()
    assert "OTHER_PERSON" not in (packet / "people.json").read_text()
    assert read_jsonl(packet / "events.jsonl") == sorted(
        read_jsonl(paths.normalized_event_path(DAY)), key=lambda r: (r["timestamp"], r["event_id"])
    )
    frozen = saved_files(packet)
    assert prepare(paths, entities) == packet
    assert saved_files(packet) == frozen and saved_files(entities.root) == before
    report = journal.inspect_packet(packet)
    assert report["people_status"] == report["input_status"] == "unchanged"
    assert "Caz" in generate._prompt(packet)


def test_selection_removes_omitted_people_and_rejects_forged_context(tmp_path):
    paths, entities, _ = fixture(tmp_path)
    packet = prepare(paths, entities)
    text, _ = journal.model_input(packet, event_ids=["evt_mail", "evt_chat"])
    view = json.loads(text)
    assert set(view["people_context"]["events"]) == {"evt_mail", "evt_chat"}
    assert "Caz" in generate._prompt(packet, text)
    assert journal._validate_model_input(packet, text)["event_ids"] == ["evt_chat", "evt_mail"]
    unknown, _ = journal.model_input(packet, event_ids=["evt_unknown"])
    assert json.loads(unknown)["people_context"]["people"] == {}
    assert "Caz" not in unknown and "spouse" not in json.dumps(
        json.loads(unknown)["people_context"]
    )
    view["people_context"]["people"]["person_partner"]["author_context"]["relationship"] = "boss"
    with pytest.raises(ValueError, match="Invalid saved model input"):
        journal._validate_model_input(packet, journal._json(view))
    with pytest.raises(ValueError, match="budget"):
        journal.model_input(packet, max_bytes=10)
    (packet / "people.json").write_text("{}\n")
    with pytest.raises(ValueError, match="people.json"):
        journal.model_input(packet)


def test_preferences_are_author_and_event_time_scoped(tmp_path):
    paths, entities, config = fixture(tmp_path)
    other = prepare(paths, entities, author="Other author")
    assert (
        "author_context"
        not in json.loads((other / "people.json").read_text())["people"]["person_partner"]
    )
    config.write_text(
        PREFERENCE.replace('valid_from = "2026-01-01"', 'valid_from = "2027-01-01"').replace(
            'valid_to = "2026-12-31"', 'valid_to = "2027-12-31"'
        )
    )
    packet = prepare(paths, entities)
    assert (
        "author_context"
        not in json.loads((packet / "people.json").read_text())["people"]["person_partner"]
    )
    config.write_text(PREFERENCE)
    upsert_resolutions(
        entities,
        [
            {
                "resolution_id": "res_email_new",
                "identity_id": "ident_email",
                "person_id": "person_other",
                "confidence": "high",
                "method": "manual_override",
                "valid_from": "2026-03-31",
                "created_at": DAY,
            }
        ],
    )
    rows = read_jsonl(paths.normalized_event_path(DAY))
    mail = next(row for row in rows if row["event_id"] == "evt_mail")
    rows.append({**mail, "event_id": "evt_earlier", "timestamp": DAY + "T12:00:00Z"})
    write_jsonl(paths.normalized_event_path(DAY), rows)
    context = json.loads((prepare(paths, entities) / "people.json").read_text())
    assert context["events"]["evt_earlier"]["ident_email"] == "person_partner"
    assert context["events"]["evt_mail"]["ident_email"] == "person_other"


@pytest.mark.parametrize(
    "change", ["names", "resolution", "name", "unrelated", "missing", "malformed"]
)
def test_live_context_drift_does_not_rewrite_frozen_packets_or_revisions(tmp_path, change):
    paths, entities, config = fixture(tmp_path)
    packet = prepare(paths, entities)
    text, _ = journal.model_input(packet, event_ids=["evt_mail"])
    checked = journal._validate_model_input(packet, text)
    revision = journal.save_journal(
        paths,
        packet_dir=packet,
        body="I replied to Caz.[fn:evt_mail]",
        model="fixture",
        input_view=text,
        generation_options={"model_input": checked},
    )
    before = {**saved_files(packet), **saved_files(revision.parent)}
    if change == "names":
        config.write_text(PREFERENCE.replace("Caz", "Case"))
    elif change == "resolution":
        upsert_resolutions(
            entities,
            [
                {
                    "resolution_id": "res_override",
                    "identity_id": "ident_email",
                    "person_id": "person_other",
                    "confidence": "high",
                    "method": "manual_override",
                    "valid_from": "2026-03-01",
                    "created_at": DAY,
                }
            ],
        )
    elif change in {"name", "unrelated"}:
        upsert_persons(
            entities,
            [
                {
                    "person_id": "person_partner" if change == "name" else "person_other",
                    "display_name": "Changed name",
                    "created_at": DAY,
                }
            ],
        )
    elif change == "missing":
        entities.database.unlink()
    else:
        config.write_text("bad = true\n")
    report = journal.inspect_packet(packet)
    expected = (
        "unchanged"
        if change == "unrelated"
        else ("unverifiable" if change in {"missing", "malformed"} else "changed")
    )
    assert report["people_status"] == report["input_status"] == expected
    assert journal.model_input(packet, event_ids=["evt_mail"])[0] == text
    generate._read_revision(revision)
    assert saved_files(packet) == {p: value for p, value in before.items() if p.parent == packet}
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in before.items())
    result = CliRunner().invoke(
        app, ["journal", "inspect", "--packet", str(packet), "--require-current"]
    )
    assert result.exit_code == (0 if change == "unrelated" else 1)
    if change not in {"unrelated", "missing", "malformed"}:
        assert prepare(paths, entities) != packet


def test_missing_root_does_not_initialize_database_and_context_is_opt_in(tmp_path):
    paths = workspace(tmp_path / "events")
    missing = RecallPaths.from_root(tmp_path / "missing")
    with pytest.raises(ValueError, match="existing Recall identity database"):
        prepare(paths, missing)
    assert not missing.root.exists()
    packet = journal.prepare_journal(paths, day=DAY, author=AUTHOR, timezone_name=TZ)
    assert "people_sha256" not in json.loads((packet / "packet.json").read_text())
    assert "people_context" not in json.loads(journal.model_input(packet)[0])
    assert not (packet / "people.json").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        PREFERENCE + 'typo = "spouse"\n',
        PREFERENCE.replace(
            'evidence = ["Author explicitly confirmed identity, relationship and personal names"]',
            "evidence = []",
        ),
        PREFERENCE.replace('preferred_names = ["Caz", "my spouse"]', 'preferred_names = "Caz"'),
        PREFERENCE + PREFERENCE,
        PREFERENCE.replace('valid_from = "2026-01-01"', 'valid_from = "bad"'),
    ],
)
def test_invalid_explicit_preferences_fail_closed(tmp_path, invalid):
    paths, entities, config = fixture(tmp_path)
    config.write_text(invalid)
    with pytest.raises(ValueError):
        prepare(paths, entities)
    assert not (paths.derived / "journal-inputs").exists()


def test_relocated_people_root_and_relevant_drift_during_preparation(tmp_path, monkeypatch):
    paths, entities, config = fixture(tmp_path)
    packet = prepare(paths, entities)
    moved = tmp_path / "moved"
    shutil.move(entities.root, moved)
    mapping = tmp_path / "relocation.json"
    mapping.write_text(json.dumps([{"old": str(entities.root), "new": str(moved)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    assert journal.inspect_packet(packet)["people_status"] == "unchanged"
    original = journal.snapshot_people
    calls = []

    def drift(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 1:
            (moved / config.relative_to(entities.root)).write_text(
                PREFERENCE.replace("Caz", "Case")
            )
        return result

    monkeypatch.setattr(journal, "snapshot_people", drift)
    with pytest.raises(ValueError, match="changed during preparation"):
        prepare(paths, entities)


def test_prepare_build_and_run_forward_context_without_real_provider(tmp_path, monkeypatch):
    paths, entities, _ = fixture(tmp_path)
    cli = CliRunner()
    common = [
        "--author",
        AUTHOR,
        "--timezone",
        TZ,
        "--root",
        str(paths.root),
        "--people-root",
        str(entities.root),
    ]
    prepared = cli.invoke(app, ["journal", "prepare", "--date", DAY, *common])
    assert prepared.exit_code == 0, prepared.output
    assert (Path(prepared.stdout.strip()) / "people.json").exists()
    calls = []

    def fake(prompt, *args):
        assert "Caz" in prompt and "Casey Example" in prompt
        calls.append(prompt)
        return "I replied to Caz.[fn:evt_mail]", {"fixture": True}

    monkeypatch.setattr(generate, "run_pi", fake)
    args = [
        "journal",
        "build",
        "--from",
        DAY,
        "--to",
        DAY,
        "--output",
        str(tmp_path / "preview"),
        *common,
    ]
    first = cli.invoke(app, args, env=runner_env(tmp_path))
    assert first.exit_code == 0 and "generated" in first.output, first.output
    assert "cached" in cli.invoke(app, args, env=runner_env(tmp_path)).output
    assert len(calls) == 1

    def replay(source, destination, **kwargs):
        write_jsonl(
            destination.normalized_event_path(DAY), read_jsonl(paths.normalized_event_path(DAY))
        )
        write_jsonl(
            destination.state / "rebuild/manifest.jsonl",
            read_jsonl(paths.state / "rebuild/manifest.jsonl"),
        )
        return [{"date": DAY, "status": "success", "source": "email"}]

    monkeypatch.setattr(run, "rebuild_range", replay)
    rows = list(
        run.run_journals(
            paths,
            workspace=tmp_path / "run",
            first=DAY,
            last=DAY,
            sources=["email"],
            author=AUTHOR,
            timezone_name=TZ,
            people_root=entities,
            **runner_config(tmp_path),
        )
    )
    assert rows[-1]["status"] == "generated" and len(calls) == 2
    assert json.loads((tmp_path / "run/run.json").read_text())["people_root"] == str(entities.root)
    result = cli.invoke(
        app,
        [
            "journal",
            "run",
            "--from",
            DAY,
            "--to",
            DAY,
            "--workspace",
            str(tmp_path / "cli-run"),
            "--source",
            "email",
            *common,
        ],
        env=runner_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 3


@pytest.mark.parametrize("stage", ["after-load", "after-projection"])
def test_projection_rejects_people_file_races(tmp_path, monkeypatch, stage):
    paths, entities, _ = fixture(tmp_path)
    packet = prepare(paths, entities)
    if stage == "after-load":
        original = journal._load_packet

        def drift_after_load(*args):
            result = original(*args)
            (packet / "people.json").write_text("{}\n")
            return result

        monkeypatch.setattr(journal, "_load_packet", drift_after_load)
    else:
        original = journal._json

        def drift_after_projection(*args):
            result = original(*args)
            (packet / "people.json").write_text("{}\n")
            return result

        monkeypatch.setattr(journal, "_json", drift_after_projection)
    with pytest.raises(ValueError, match="changed"):
        journal.model_input(packet)


def test_input_export_cannot_write_into_people_source_data(tmp_path):
    paths, entities, _ = fixture(tmp_path)
    packet = prepare(paths, entities)
    target = entities.data / "view.json"
    result = CliRunner().invoke(
        app, ["journal", "input-view", "--packet", str(packet), "--output", str(target)]
    )
    assert result.exit_code != 0 and not target.exists()


@pytest.mark.parametrize("invalid", ["missing", "malformed", "overlap"])
def test_run_rejects_bad_people_root_before_capture_or_workspace_writes(
    tmp_path, monkeypatch, invalid
):
    paths, entities, config = fixture(tmp_path)
    target = tmp_path / "run"
    if invalid == "missing":
        entities = RecallPaths.from_root(tmp_path / "missing")
    elif invalid == "malformed":
        config.write_text("invalid = true\n")
    else:
        target = entities.root / "run"
    monkeypatch.setattr(run, "backfill_slack", lambda *a, **kw: pytest.fail("No capture"))
    with pytest.raises(ValueError):
        list(
            run.run_journals(
                paths,
                workspace=target,
                first=DAY,
                last=DAY,
                sources=["slack"],
                author=AUTHOR,
                timezone_name=TZ,
                people_root=entities,
                slack_client=History(),
                **runner_config(tmp_path),
            )
        )
    assert not target.exists()


def test_naming_change_invalidates_build_cache_without_editing_old_revision(tmp_path, monkeypatch):
    paths, entities, config = fixture(tmp_path)
    calls = []

    def fake(prompt, *args):
        calls.append(prompt)
        return "I confirmed the enrollment.[fn:evt_mail]", {"fixture": True}

    monkeypatch.setattr(generate, "run_pi", fake)
    options: dict[str, Any] = dict(
        first=DAY,
        last=DAY,
        author=AUTHOR,
        timezone_name=TZ,
        output=tmp_path / "preview",
        people_root=entities,
        **runner_config(tmp_path),
    )
    first = generate.build_journals(paths, **options)[0]
    frozen = saved_files(Path(first["revision"]).parent)
    assert generate.build_journals(paths, **options)[0]["status"] == "cached"
    config.write_text(PREFERENCE.replace("Caz", "Case"))
    changed = generate.build_journals(paths, **options)[0]
    assert changed["status"] == "generated" and len(calls) == 2
    assert changed["revision"] != first["revision"] and "Case" in calls[-1]
    assert saved_files(Path(first["revision"]).parent) == frozen
