from __future__ import annotations

import hashlib
import json
from email.message import EmailMessage

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-10-01"
SELF = "ident_email_alex_example_org"
OTHER = "ident_email_pat_example_org"
runner = CliRunner()


def fixture(tmp_path, body="I checked the figures. They look right to me.", **changes):
    paths = RecallPaths.from_root(tmp_path)
    message = EmailMessage()
    message["From"] = "Alex Writer <alex@example.org>"
    message["To"] = "Pat <pat@example.org>"
    message["Message-ID"] = "<voice-fixture@example.org>"
    message["Subject"] = "Figures"
    message.set_content(body)
    raw = paths.raw / "email/fixture.eml"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(message.as_bytes())
    event = {
        "event_id": "evt_voice",
        "source": "email",
        "kind": "email",
        "account": "work",
        "date": DAY,
        "timestamp": DAY + "T18:00:00Z",
        "sender_identity_id": SELF,
        "participant_identity_ids": [SELF, OTHER],
        "text": body.strip(),
        "tags": ["email"],
        "raw_ref": {
            "source": "email",
            "path": paths.relative_to_root(raw),
            "locator": {"message_id": "voice-fixture@example.org"},
        },
        **changes,
    }
    write_jsonl(paths.normalized_event_path(DAY), [event])
    return paths, raw, message, event


def command(paths, *extra, identity=SELF):
    return runner.invoke(
        app,
        [
            "voice",
            "candidates",
            "--root",
            str(paths.root),
            "--identity",
            identity,
            "--from",
            DAY,
            "--to",
            DAY,
            *extra,
        ],
    )


def report(paths, *extra):
    result = command(paths, *extra)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cli_requires_identity_and_bounded_dates_without_creating_store(tmp_path):
    missing = tmp_path / "absent"
    for args in ([], ["--identity", SELF], ["--from", DAY, "--to", DAY]):
        result = runner.invoke(app, ["voice", "candidates", "--root", str(missing), *args])
        assert result.exit_code != 0
    assert not missing.exists()


def test_clean_email_is_pending_review_not_confirmed_corpus_and_keeps_provenance(tmp_path):
    paths, raw, _, event = fixture(tmp_path)
    normalized = paths.normalized_event_path(DAY)
    normalized.write_text("\n\n" + normalized.read_text())
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths.root.rglob("*") if p.is_file()
    }
    value = report(paths)
    assert value["counts"] == {"needs_review": 1, "excluded": 0}
    item = value["records"][0]
    assert item["status"] == "needs_review" and item["corpus_eligible"] is False
    assert item["identity_id"] == SELF and item["account"] == "work"
    assert item["line"] == 3 and item["event_id"] == event["event_id"]
    assert item["normalized_path"] == item["resolved_normalized_path"] == str(normalized)
    assert item["raw_path"] == str(raw)
    assert item["raw_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    passage = item["passage"]
    assert passage["text"] == event["text"]
    assert passage["sha256"] == hashlib.sha256(event["text"].encode()).hexdigest()
    assert passage["start"] == 0 and passage["end"] == len(event["text"])
    assert "human" in value["note"].lower()
    assert not paths.database.exists()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


@pytest.mark.parametrize(
    "tail,reason",
    [
        ("On Tue, Pat wrote:\n> OTHER AUTHOR SECRET", "quoted_tail"),
        ("On Tue, Pat\n<pat@example.org> wrote:\n> OTHER AUTHOR SECRET", "quoted_tail"),
        ("---------- Forwarded message ---------\nOTHER AUTHOR SECRET", "forwarded_tail"),
        ("-----Original Message-----\nFrom: Pat\nOTHER AUTHOR SECRET", "forwarded_tail"),
        ("From: Pat <pat@example.org>\nSent: Tuesday\nOTHER AUTHOR SECRET", "forwarded_tail"),
        ("> OTHER AUTHOR SECRET\n\nMy inline answer", "quoted_tail"),
        ("-- \nAlex Writer\nSIGNATURE SECRET", "signature"),
        ("Regards,\nAlex Writer\nSIGNATURE SECRET", "signature"),
    ],
)
def test_excludes_recognized_tails_with_exact_plain_body_offsets(tmp_path, tail, reason):
    original = "  I have checked it.\n\n"
    paths, _, _, _ = fixture(tmp_path, original + tail)
    value = report(paths)
    item = value["records"][0]
    assert item["status"] == "needs_review"
    assert item["passage"]["text"] == "I have checked it."
    assert item["passage"]["start"] == 2
    assert item["passage"]["end"] == len(original.rstrip())
    assert item["exclusions"][0]["reason"] == reason
    assert "OTHER AUTHOR SECRET" not in json.dumps(value)
    assert "SIGNATURE SECRET" not in json.dumps(value)


@pytest.mark.parametrize(
    "body,reason",
    [
        ("> Quoted message", "no_original_passage"),
        ("---------- Forwarded message ---------\nSomeone else's words", "no_original_passage"),
        ('Pat said "use this sentence".', "ambiguous_quotation"),
        ("Pat said “use this sentence”.", "ambiguous_quotation"),
        ("```python\nprint('not prose')\n```", "non_prose"),
        ("", "no_original_passage"),
    ],
)
def test_ambiguous_or_empty_text_never_proposes_a_passage(tmp_path, body, reason):
    paths, _, _, _ = fixture(tmp_path, body)
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["corpus_eligible"] is False
    assert item["reason"] == reason and item["passage"] is None


@pytest.mark.parametrize(
    "header,value,reason",
    [
        ("Subject", "Fwd: Figures", "forward_boundary_unknown"),
        ("In-Reply-To", "<prior@example.org>", "reply_boundary_unknown"),
        ("Auto-Submitted", "auto-generated", "automated"),
        ("Precedence", "bulk", "automated"),
        ("List-Id", "newsletter.example.org", "automated"),
    ],
)
def test_raw_headers_gate_automated_or_unbounded_reply_forward(tmp_path, header, value, reason):
    paths, raw, message, _ = fixture(tmp_path, "PRIVATE EXCLUDED TEXT")
    if header in message:
        message.replace_header(header, value)
    else:
        message[header] = value
    raw.write_bytes(message.as_bytes())
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["reason"] == reason
    assert item["passage"] is None


@pytest.mark.parametrize(
    "tag,reason",
    [
        ("automated", "automated"),
        ("bulk", "automated"),
        ("marketing", "automated"),
        ("ai_generated", "known_generated"),
        ("ai_assisted", "known_generated"),
    ],
)
def test_tagged_generated_and_bulk_text_is_excluded_before_raw_access(tmp_path, tag, reason):
    paths, raw, _, _ = fixture(tmp_path, "DO NOT OUTPUT", tags=[tag])
    raw.unlink()
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["reason"] == reason
    assert item["passage"] is None


def test_explicit_known_generated_exclusion_and_sender_only_selection(tmp_path):
    paths, _, _, _ = fixture(tmp_path, "AI DRAFT")
    item = report(paths, "--exclude-event", "evt_voice")["records"][0]
    assert item["reason"] == "explicitly_excluded" and item["passage"] is None
    result = command(paths, identity=OTHER)
    assert json.loads(result.stdout)["records"] == []
    assert report(paths, "--account", "personal")["records"] == []


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("missing", "raw_unverifiable"),
        ("sender", "raw_sender_mismatch"),
        ("id", "raw_message_id_mismatch"),
        ("text", "raw_text_mismatch"),
        ("html", "plain_body_unverifiable"),
        ("multiple", "plain_body_unverifiable"),
    ],
)
def test_unverifiable_raw_or_body_is_excluded(tmp_path, damage, reason):
    paths, raw, message, event = fixture(tmp_path)
    if damage == "missing":
        raw.unlink()
    elif damage == "sender":
        message.replace_header("From", "Someone <other@example.org>")
        raw.write_bytes(message.as_bytes())
    elif damage == "id":
        message.replace_header("Message-ID", "<other@example.org>")
        raw.write_bytes(message.as_bytes())
    elif damage == "text":
        event["text"] = "Invented normalized text"
        write_jsonl(paths.normalized_event_path(DAY), [event])
    elif damage == "html":
        message.set_content("<p>Text</p>", subtype="html")
        raw.write_bytes(message.as_bytes())
    else:
        message.make_mixed()
        part = EmailMessage()
        part.set_content("Second inline body")
        message.attach(part)
        raw.write_bytes(message.as_bytes())
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["reason"] == reason
    assert item["passage"] is None


def test_messages_and_journals_without_supported_attribution_are_not_admitted(tmp_path):
    paths, _, _, event = fixture(tmp_path)
    for source, kind in [("telegram", "message"), ("slack", "message"), ("recall", "journal")]:
        write_jsonl(paths.normalized_event_path(DAY), [{**event, "source": source, "kind": kind}])
        item = report(paths)["records"][0]
        assert item["status"] == "excluded" and item["reason"] == "unsupported_source"
        assert item["passage"] is None


def test_limit_missing_days_bad_records_and_safe_terminal_escaping(tmp_path):
    paths, _, _, event = fixture(tmp_path, "Original \x1b[31m words")
    write_jsonl(paths.normalized_event_path(DAY), [event, {**event, "event_id": "evt_two"}])
    value = report(paths, "--limit", "1", "--to", "2026-10-02")
    assert value["truncated"] and len(value["records"]) == 1
    assert "\x1b" not in command(paths).stdout
    write_jsonl(paths.normalized_event_path(DAY), [event])
    value = report(paths, "--to", "2026-10-02")
    assert value["missing_days"] == ["2026-10-02"]
    paths.normalized_event_path(DAY).write_text('{"FAKE_SECRET":')
    result = command(paths)
    assert result.exit_code != 0 and "FAKE_SECRET" not in result.output
    assert "invalid" in result.output.lower()


def test_invalid_scope_does_not_write_or_create_a_root(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "absent")
    for extra in (["--to", "2026-09-30"], ["--from", "bad-date"], ["--identity", "   "]):
        result = command(paths, *extra)
        assert result.exit_code != 0
    assert not paths.root.exists()
