from __future__ import annotations

import hashlib
from email import policy

import pytest
from test_voice import DAY, fixture, report

from recall.storage.jsonl import write_jsonl


@pytest.mark.parametrize(
    "tail",
    [
        "Alex",
        "Alex Writer",
        "  Alex",
        "\tAlex Writer\n",
        "Thanks,\n  Alex",
        " Thank you,\n Alex",
        "Regards,\n\n  Alex Writer",
    ],
)
@pytest.mark.parametrize("quoted", [False, True])
def test_observed_bare_and_indented_signoffs_do_not_enter_passage(tmp_path, tail, quoted):
    original = "I checked the figures. They look right."
    body = original + "\n\n" + tail
    if quoted:
        body += "\n\nOn Tue, Pat wrote:\n> OTHER AUTHOR TEXT"
    paths, raw, _, _ = fixture(tmp_path, body)
    before = raw.read_bytes()
    value = report(paths)
    item = value["records"][0]
    assert item["status"] == "needs_review" and item["corpus_eligible"] is False
    assert item["passage"]["text"] == original
    assert item["passage"]["start"] == 0 and item["passage"]["end"] == len(original)
    assert item["passage"]["sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert item["exclusions"][0]["reason"] in {"signature", "sender_name_tail"}
    assert raw.read_bytes() == before
    assert value["extractor_version"] == 2


@pytest.mark.parametrize("closing", ["Rosa", "  Rosa Example", " Regards,\n\n Rosa"])
def test_different_raw_sender_name_and_crlf_do_not_change_original_offsets(tmp_path, closing):
    original = "  Café looks good."
    body = original + "\n\n" + closing + "\n\nOn Tue, Pat wrote:\n> OLD TEXT"
    paths, raw, message, event = fixture(tmp_path, body)
    message.replace_header("From", "Rosa Example <alex@example.org>")
    raw.write_bytes(message.as_bytes(policy=policy.SMTP))
    event["text"] = body.replace("\n", "\r\n").strip()
    write_jsonl(paths.normalized_event_path(DAY), [event])
    item = report(paths)["records"][0]
    assert item["passage"]["text"] == original.strip()
    assert item["passage"]["start"] == 2 and item["passage"]["end"] == len(original)
    assert item["corpus_eligible"] is False


def test_sender_name_context_is_generic_and_not_removed_inside_prose(tmp_path):
    body = "I spoke to Alex Writer about it.\n\nAlex Writer will send the report."
    paths, raw, message, event = fixture(tmp_path, body)
    item = report(paths)["records"][0]
    assert item["passage"]["text"] == body
    assert item["exclusions"] == []


def test_other_names_are_not_assumed_to_be_self_signoffs(tmp_path):
    body = "Here is the name.\n\nPat"
    paths, _, _, _ = fixture(tmp_path, body)
    assert report(paths)["records"][0]["passage"]["text"] == body


@pytest.mark.parametrize(
    "prefix",
    [
        "Oct 2, 2026 8:19:02 AM Pat Example <pat@example.org>:",
        "[Image]\n\nOct 2, 2026 8:19:02 AM Pat Example <pat@example.org>:",
        "[Image]",
        "Contact <pat@example.org>:",
    ],
)
def test_unhandled_attribution_or_image_metadata_rejects_the_whole_candidate(tmp_path, prefix):
    body = "I checked this for you.\n\n" + prefix + "\n> OTHER AUTHOR TEXT"
    paths, raw, _, _ = fixture(tmp_path, body)
    before = raw.read_bytes()
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["reason"] == "ambiguous_attribution"
    assert item["passage"] is None and item["corpus_eligible"] is False
    assert raw.read_bytes() == before


def test_matching_signature_only_has_no_original_passage(tmp_path):
    paths, _, _, _ = fixture(tmp_path, "Alex Writer")
    item = report(paths)["records"][0]
    assert item["status"] == "excluded" and item["reason"] == "no_original_passage"
    assert item["passage"] is None


def test_matching_name_without_separate_paragraph_is_not_guessed(tmp_path):
    body = "The next person to speak is\nAlex"
    paths, _, _, _ = fixture(tmp_path, body)
    assert report(paths)["records"][0]["passage"]["text"] == body
