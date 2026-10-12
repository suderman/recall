import json

import pytest
from test_journals import DAY, TZ, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.synthesize import journal
from recall.synthesize.selection import RULE, default_selection


def _email(event_id, hour, tags, text="Short note."):
    return {
        "event_id": event_id,
        "date": DAY,
        "source": "email",
        "kind": "email",
        "timestamp": f"2026-03-30T{hour:02d}:00:00-06:00",
        "text": text,
        "raw_ref": None,
        "tags": ["email", *tags],
    }


def packet(tmp_path):
    paths = workspace(tmp_path / "inputs")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows += [
        _email("evt_news", 9, ["bulk", "marketing", "non_conversational"]),
        _email("evt_receipt", 10, ["automated", "transactional", "non_conversational"]),
        _email("evt_blank", 11, ["conversation_candidate", "empty_body"], text=""),
        _email("evt_thread", 12, ["conversation_candidate"], text="Long history. " * 400),
        _email("evt_reply", 13, ["conversation_candidate"]),
    ]
    write_jsonl(paths.normalized_event_path(DAY), rows)
    return journal.prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)


def test_default_rule_drops_bulk_and_automated_mail_and_keeps_the_rest(tmp_path):
    directory = packet(tmp_path)

    selected, omitted = default_selection(directory, max_bytes=1024 * 1024)

    assert selected == ["evt_break", "evt_thread", "evt_reply", "evt_mail"]
    assert omitted == {
        "evt_blank": "email:empty_body",
        "evt_news": "email:bulk",
        "evt_receipt": "email:automated",
    }


def test_default_rule_drops_largest_email_first_to_fit_the_budget(tmp_path):
    directory = packet(tmp_path)
    _, full = journal.model_input(
        directory,
        event_ids=["evt_break", "evt_mail", "evt_reply", "evt_thread"],
        max_bytes=1024 * 1024,
    )

    selected, omitted = default_selection(directory, max_bytes=full["bytes"] - 1)

    assert selected == ["evt_break", "evt_reply", "evt_mail"]
    assert omitted["evt_thread"] == "over_budget"
    assert journal.model_input(directory, event_ids=selected, max_bytes=full["bytes"] - 1)


def test_default_rule_never_drops_non_email_to_fit(tmp_path):
    directory = packet(tmp_path)

    with pytest.raises(ValueError, match="budget"):
        default_selection(directory, max_bytes=200)


def test_input_view_cli_reports_rule_and_reasons_without_prose(tmp_path):
    directory = packet(tmp_path)

    result = CliRunner().invoke(
        app, ["journal", "input-view", "--packet", str(directory), "--select", "default"]
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["selection"] == {
        "rule": RULE,
        "omitted": {
            "evt_blank": "email:empty_body",
            "evt_news": "email:bulk",
            "evt_receipt": "email:automated",
        },
    }
    assert summary["events"] == 4
    assert "Long history" not in result.output


def test_input_view_refuses_both_explicit_events_and_a_rule(tmp_path):
    directory = packet(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "journal",
            "input-view",
            "--packet",
            str(directory),
            "--select",
            "default",
            "--event",
            "evt_mail",
        ],
    )

    assert result.exit_code != 0
    assert "either" in result.output
