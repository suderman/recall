import json
import shutil

import pytest

from recall.connectors.calendar.khal import build_khal_command, load_calendar_events
from recall.connectors.email.notmuch import build_notmuch_query, load_email_messages
from recall.normalize.time import day_bounds


# Use past transitions; future timezone rules can change.
@pytest.mark.parametrize("day,hours", [("2025-03-09", 23), ("2025-11-02", 25)])
def test_email_query_uses_exact_local_day_across_dst(day, hours):
    start, end = day_bounds(day, "America/Edmonton")
    assert end.timestamp() - start.timestamp() == hours * 3600
    assert build_notmuch_query(day, timezone_name="America/Edmonton") == (
        f"date:@{int(start.timestamp())}..@{int(end.timestamp()) - 1} and not tag:deleted"
    )


def test_email_filters_boundary_records_and_keeps_local_date(tmp_path):
    files = []
    for index, timestamp in enumerate(
        [
            "Tue, 31 Mar 2026 05:59:59 +0000",
            "Tue, 31 Mar 2026 06:00:00 +0000",
            "Wed, 01 Apr 2026 05:59:59 +0000",
            "Wed, 01 Apr 2026 06:00:00 +0000",
            "broken",
        ]
    ):
        path = tmp_path / f"{index}.eml"
        path.write_text(f"Message-ID: <{index}@example.com>\nDate: {timestamp}\n\nEvidence\n")
        files.append(str(path))
    rows = load_email_messages(
        date="2026-03-31", timezone_name="America/Edmonton", runner=lambda _: "\n".join(files)
    )
    assert {row.message_id for row in rows} == {"1@example.com", "2@example.com", "4@example.com"}
    assert {row.date for row in rows} == {"2026-03-31"}
    assert next(row for row in rows if row.message_id == "4@example.com").timestamp == (
        "2026-03-31T06:00:00Z"
    )


def test_khal_options_have_correct_scope_and_repeated_json_flags():
    command = build_khal_command(date="2026-03-31", calendars=["work"], config_path="config")
    assert command[:4] == ["khal", "-c", "config", "list"]
    assert command.index("-a") > command.index("list")
    assert command.count("--json") == 14
    assert command[-2:] == ["2026-03-31", "2026-04-01"]


def test_calendar_bounds_and_recurring_ids_are_stable():
    rows = [
        {
            "uid": "weekly",
            "start": "2026-03-31T23:00:00-06:00",
            "end": "2026-04-01T00:30:00-06:00",
            "repeat-pattern": "FREQ=WEEKLY",
        },
        {
            "uid": "outside",
            "start": "2026-04-01T00:00:00-06:00",
            "end": "2026-04-01T01:00:00-06:00",
        },
        {"uid": "day", "start": "2026-03-31", "end": "2026-04-01", "all-day": True},
    ]

    def loader(_):
        return json.dumps(rows)

    first = load_calendar_events(date="2026-03-31", timezone_name="America/Edmonton", runner=loader)
    second = load_calendar_events(
        date="2026-04-01", timezone_name="America/Edmonton", runner=loader
    )
    assert {row.uid for row in first} == {"weekly", "day"}
    assert first[1].occurrence_id == second[0].occurrence_id
    assert first[0].start == "2026-03-31T00:00:00-06:00"


@pytest.mark.skipif(shutil.which("khal") is None, reason="khal CLI is not installed")
def test_real_khal_with_non_iso_locale_all_day_and_recurrence(tmp_path):
    calendar = tmp_path / "calendar"
    calendar.mkdir()
    for uid, body in {
        "weekly": "DTSTART:20260324T150000Z\nDTEND:20260324T160000Z\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\nSUMMARY:Weekly\nSTATUS:CANCELLED\n",
        "day": "DTSTART;VALUE=DATE:20260331\nDTEND;VALUE=DATE:20260401\nSUMMARY:All day\n",
    }.items():
        (calendar / f"{uid}.ics").write_text(
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Recall tests//EN\n"
            f"BEGIN:VEVENT\nUID:{uid}\n{body}END:VEVENT\nEND:VCALENDAR\n"
        )
    config = tmp_path / "config"
    content = (
        f"[calendars]\n[[test]]\npath = {calendar}\nreadonly = True\n"
        f"[sqlite]\npath = {tmp_path / 'cache.db'}\n"
        "[locale]\nlocal_timezone = America/Edmonton\ndefault_timezone = America/Edmonton\n"
        "dateformat = %m/%d/%Y\nlongdateformat = %m/%d/%Y\ntimeformat = %I:%M:%S %p\n"
        "datetimeformat = %a %d %b %Y %I:%M:%S %p\n"
        "longdatetimeformat = %a %d %b %Y %I:%M:%S %p\n"
    )
    config.write_text(content)
    rows = load_calendar_events(
        date="2026-03-31",
        config_path=str(config),
        timezone_name="America/Edmonton",
        calendars=["test"],
    )
    assert {row.uid for row in rows} == {"day", "weekly"}
    day = next(row for row in rows if row.uid == "day")
    assert day.start == "2026-03-31T00:00:00-06:00"
    assert day.end == "2026-04-01T00:00:00-06:00"
    weekly = next(row for row in rows if row.uid == "weekly")
    assert weekly.start == "2026-03-31T15:00:00Z"
    assert {"canceled", "recurring"} <= set(weekly.tags)
    assert config.read_text() == content
