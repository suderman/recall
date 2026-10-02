from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from recall.connectors.telegram.normalize import _extract_text_and_tags, normalize_telegram_day
from recall.search import render_results
from recall.storage.paths import RecallPaths
from recall.synthesize.timeline import render_day

CASES = json.loads(
    (Path(__file__).parent / "fixtures/telegram/service_contents.json").read_text(encoding="utf-8")
)
DAY = "2026-04-02"


@pytest.mark.parametrize("case", CASES)
def test_service_content_contract(case):
    assert _extract_text_and_tags(case["content"]) == (
        case["text"],
        ["message", "telegram", case["tag"]],
    )


@pytest.mark.parametrize("duration", [None, -1, True, False, "12", 1.5])
def test_call_does_not_report_invalid_duration(duration):
    assert _extract_text_and_tags({"@type": "messageCall", "duration": duration}) == (
        "Telegram call",
        ["message", "telegram", "call"],
    )


@pytest.mark.parametrize("is_video", [None, 0, 1, "false"])
def test_call_does_not_infer_audio_or_video_from_non_boolean(is_video):
    assert _extract_text_and_tags({"@type": "messageCall", "is_video": is_video}) == (
        "Telegram call",
        ["message", "telegram", "call"],
    )


@pytest.mark.parametrize("discard_reason", [None, {}, {"@type": ""}])
def test_call_does_not_invent_missing_discard_reason(discard_reason):
    assert _extract_text_and_tags({"@type": "messageCall", "discard_reason": discard_reason}) == (
        "Telegram call",
        ["message", "telegram", "call"],
    )


@pytest.mark.parametrize(
    ("content_type", "tag"),
    [
        ("messagePhoto", "photo"),
        ("messageDocument", "document"),
        ("messageVideo", "video"),
        ("messageAnimation", "animation"),
        ("messageAudio", "audio"),
        ("messageVoiceNote", "voice_note"),
        ("messageVideoNote", "video_note"),
    ],
)
@pytest.mark.parametrize("caption", ["", "caption https://example.com/photo"])
def test_media_captions_keep_existing_behavior(content_type, tag, caption):
    assert _extract_text_and_tags({"@type": content_type, "caption": {"text": caption}}) == (
        caption,
        ["message", "telegram", tag],
    )


@pytest.mark.parametrize(
    ("content", "text", "tags"),
    [
        (
            {"@type": "messageText", "text": {"text": "hello\nhttps://example.com"}},
            "hello\nhttps://example.com",
            ["message", "telegram", "text"],
        ),
        (
            {"@type": "messageSticker", "sticker": {"emoji": " 🙂 "}},
            "🙂",
            ["message", "telegram", "sticker"],
        ),
        (
            {"@type": "messageOther", "caption": {"text": "saved caption"}},
            "saved caption",
            ["message", "telegram"],
        ),
    ],
)
def test_other_content_keeps_existing_behavior(content, text, tags):
    assert _extract_text_and_tags(content) == (text, tags)


def _normalize_content(tmp_path, content):
    paths = RecallPaths.from_root(tmp_path)
    raw_dir = paths.raw_capture_dir("telegram", DAY)
    raw_dir.mkdir(parents=True)
    raw = raw_dir / "updates.jsonl"
    raw.write_text(
        json.dumps(
            {
                "source": "telegram",
                "account": "personal",
                "capture_mode": "fixture",
                "received_at": "2026-04-02T10:00:00Z",
                "update_type": "updateNewMessage",
                "payload": {
                    "chat": {"id": 1001, "title": "Fixture chat", "participant_user_ids": [42]},
                    "message": {
                        "id": 9001,
                        "chat_id": 1001,
                        "date": 1774976467,
                        "sender_id": {"@type": "messageSenderUser", "user_id": 99},
                        "reply_to_message_id": 8000,
                        "forward_info": {"origin": {"@type": "messageForwardOriginUser"}},
                        "media_album_id": 777,
                        "content": content,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    original = raw.read_bytes()
    event_path, artifact_path = normalize_telegram_day(paths, date=DAY)
    event_bytes, artifact_bytes = event_path.read_bytes(), artifact_path.read_bytes()
    normalize_telegram_day(paths, date=DAY)
    assert raw.read_bytes() == original
    assert event_path.read_bytes() == event_bytes
    assert artifact_path.read_bytes() == artifact_bytes
    return paths, json.loads(event_path.read_text()), artifact_bytes


@pytest.mark.parametrize("case", CASES)
def test_service_normalization_preserves_event_contract_and_no_artifacts(tmp_path, case):
    _, event, artifacts = _normalize_content(tmp_path, case["content"])
    assert event == {
        "event_id": "evt_" + hashlib.sha256(b"telegram:personal:1001:9001").hexdigest()[:20],
        "source": "telegram",
        "account": "personal",
        "timestamp": "2026-03-31T17:01:07Z",
        "date": DAY,
        "kind": "message",
        "conversation_id": "1001",
        "conversation_label": "Fixture chat",
        "thread_id": "reply:8000",
        "sender_identity_id": "ident_telegram_user_99",
        "sender_person_id": None,
        "participant_identity_ids": ["ident_telegram_user_42", "ident_telegram_user_99"],
        "participant_person_ids": [],
        "text": case["text"],
        "source_urls": [],
        "artifact_ids": [],
        "raw_ref": {
            "source": "telegram",
            "path": f"data/raw/telegram/{DAY}/updates.jsonl",
            "locator": {"line": 1, "message_id": 9001, "chat_id": 1001},
        },
        "raw_fragment": None,
        "tags": ["message", "telegram", case["tag"], "reply", "forwarded", "album"],
    }
    assert artifacts == b""


@pytest.mark.parametrize("content_type", ["messageAnimatedEmoji", "messageCall"])
def test_service_source_text_is_quoted_in_org_views(tmp_path, content_type):
    untrusted = (
        '🙋🏽‍♀️\n* Forged heading\n#+begin_src emacs-lisp\n(error "executed")\n#+end_src\n'
        ':PROPERTIES:\n:END:\nLocal Variables:\neval: (error "executed")\nEnd:\x1b'
    )
    content = {"@type": content_type}
    if content_type == "messageAnimatedEmoji":
        content["emoji"] = untrusted
        expected = untrusted
    else:
        content["discard_reason"] = {"@type": untrusted}
        expected = "Telegram call; discard reason: " + untrusted
    paths, event, _ = _normalize_content(tmp_path, content)
    assert event["text"] == expected
    timeline = render_day(paths, DAY, "UTC", [])
    search = render_results(
        [
            {
                "event": event,
                "snippet": event["text"],
                "dates": [DAY],
                "observed_identity_labels": [],
                "normalized_path": str(paths.normalized_event_path(DAY)),
                "line": 1,
            }
        ],
        org=True,
    )
    for rendered in [timeline, search]:
        assert "🙋🏽‍♀️" in rendered
        assert "\n: * Forged heading\n" in rendered
        assert "\n: #+begin_src emacs-lisp\n" in rendered
        assert "\n: :PROPERTIES:\n" in rendered
        assert "\n: Local Variables\\:\n" in rendered
        assert "\\u001b" in rendered
        assert "\n* Forged heading\n" not in rendered
        assert "\n#+begin_src emacs-lisp\n" not in rendered
