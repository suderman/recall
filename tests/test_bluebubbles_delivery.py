from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import recall.connectors.bluebubbles.capture as capture
from recall.connectors.bluebubbles.config import BlueBubblesSourceConfig
from recall.connectors.bluebubbles.entities import sync_bluebubbles_entities
from recall.connectors.bluebubbles.normalize import _identity_id, normalize_bluebubbles_day
from recall.connectors.bluebubbles.recovery import recover_bluebubbles_messages
from recall.connectors.bluebubbles.webhook import create_bluebubbles_webhook_app
from recall.storage.jsonl import read_jsonl
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor

SECRET = "FAKE_SECRET"
SINCE = "2026-03-31T00:00:00Z"
UNTIL = "2026-04-01T00:00:00Z"


def message(guid="fixture", hour=10):
    return {
        "guid": guid,
        "dateCreated": int(datetime(2026, 3, 31, hour, tzinfo=timezone.utc).timestamp() * 1000),
        "text": "fixture message",
        "chatGuid": "fixture-chat",
    }


def client(paths):
    return TestClient(
        create_bluebubbles_webhook_app(paths, BlueBubblesSourceConfig(webhook_token=SECRET)),
        raise_server_exceptions=False,
    )


def cursor(paths, account="personal"):
    return get_connector_cursor(
        paths, source="bluebubbles", account=account, cursor_key="last_message_timestamp"
    )


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def post(self, url, *, json):
        self.calls.append(json)
        payload = next(self.pages)
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(200, request=httpx.Request("POST", url), json=payload)

    def close(self):
        pass


def recover(paths, pages, **kwargs):
    return recover_bluebubbles_messages(
        paths,
        account="personal",
        server_url="https://fake.invalid",
        password=SECRET,
        client=pages,
        since=SINCE,
        until=UNTIL,
        **kwargs,
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"type": 7},
        {"type": "new-message", "data": []},
        {"type": "new-message", "data": {}},
        {"type": "new-message", "data": {"guid": "x", "dateCreated": "bad"}},
    ],
)
def test_webhook_bad_shape_is_client_error_without_capture(tmp_path, payload):
    paths = RecallPaths.from_root(tmp_path)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert SECRET not in response.text
    assert not list(paths.raw.rglob("events.jsonl"))


def test_webhook_bad_json_and_storage_failure_are_safe(tmp_path, monkeypatch):
    paths = RecallPaths.from_root(tmp_path)
    response = client(paths).post("/bluebubbles/webhook", params={"token": SECRET}, content="{")
    assert response.status_code == 400

    def failed(*args, **kwargs):
        raise OSError(f"write failed {SECRET}")

    monkeypatch.setattr(capture, "write_jsonl", failed)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "new-message", "data": message()},
    )
    assert response.status_code == 503 and SECRET not in response.text
    assert cursor(paths) is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": {}},
        {"data": ["bad"]},
        {"data": [message(), {}]},
        {"data": [{"guid": "no-date"}]},
        {"data": [{"guid": "x", "dateCreated": "bad"}]},
    ],
)
def test_invalid_recovery_page_fails_before_page_capture(tmp_path, payload):
    paths = RecallPaths.from_root(tmp_path)
    with pytest.raises(RuntimeError):
        recover(paths, Pages([payload]))
    assert cursor(paths) is None
    assert not list(paths.raw.rglob("events.jsonl"))


def test_recovery_partial_failure_retains_cursor_and_restart_recovers_gap(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    # A late page can precede an earlier message on another page after restart.
    with pytest.raises(RuntimeError):
        recover(paths, Pages([{"data": [message("late", 20)]}, RuntimeError(SECRET)]), page_size=1)
    assert cursor(paths) is None
    result = recover(
        paths,
        Pages([{"data": [message("early", 10)]}, {"data": [message("late", 20)]}, {"data": []}]),
        page_size=1,
    )
    assert result.recovered_messages == 1 and result.skipped_existing == 1
    stored = cursor(paths)
    assert stored is not None and stored.cursor_value == "2026-03-31T20:00:00Z"
    assert len([r for p in paths.raw.rglob("events.jsonl") for r in read_jsonl(p)]) == 2


def test_recovery_guid_checks_all_receipt_dates_but_not_other_accounts(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    capture.append_bluebubbles_event(
        paths,
        account="work",
        received_at="2026-04-02T12:00:00Z",
        payload={"type": "new-message", "data": message()},
    )
    first = recover(paths, Pages([{"data": [message()]}]))
    assert first.recovered_messages == 1
    # Message was received on a later day, so a date-only scan would miss it.
    capture.append_bluebubbles_event(
        paths,
        account="personal",
        received_at="2026-04-03T12:00:00Z",
        payload={"type": "new-message", "data": message("other")},
    )
    second = recover(paths, Pages([{"data": [message("other")]}]))
    assert second.recovered_messages == 0 and second.skipped_existing == 1


def test_corrupt_raw_log_stops_recovery_without_cursor_update(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    path = paths.raw_capture_dir("bluebubbles", "2026-03-31") / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"partial":')
    with pytest.raises(RuntimeError):
        recover(paths, Pages([{"data": [message()]}]))
    assert path.read_text() == '{"partial":' and cursor(paths) is None


@pytest.mark.parametrize("failed_sync", [False, True])
def test_raw_sync_precedes_cursor_and_acknowledgement(tmp_path, monkeypatch, failed_sync):
    paths = RecallPaths.from_root(tmp_path)
    calls = []
    fsync = os.fsync
    advance = capture.advance_bluebubbles_cursor

    def sync(fd):
        calls.append("fsync")
        if failed_sync:
            raise OSError("injected sync failure")
        return fsync(fd)

    def checkpoint(*args, **kwargs):
        calls.append("cursor")
        assert calls[:2] == ["fsync", "fsync"]
        return advance(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(capture, "advance_bluebubbles_cursor", checkpoint)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "new-message", "data": message()},
    )
    assert response.status_code == (503 if failed_sync else 200)
    assert (cursor(paths) is None) == failed_sync
    if failed_sync:
        assert calls == ["fsync"]
    else:
        assert calls == ["fsync", "fsync", "cursor"]
    # A failed sync must not delete raw evidence already written.
    assert len([r for p in paths.raw.rglob("events.jsonl") for r in read_jsonl(p)]) == 1


def test_busy_writer_blocks_both_capture_routes_and_releases(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    pages = Pages([{"data": [message()]}])
    with capture.bluebubbles_writer(paths):
        response = client(paths).post(
            "/bluebubbles/webhook",
            params={"token": SECRET},
            json={"type": "new-message", "data": message()},
        )
        assert response.status_code == 503
        with pytest.raises(RuntimeError, match="writer is busy"):
            recover(paths, pages)
        assert pages.calls == []
        assert not list(paths.raw.rglob("events.jsonl"))
    assert recover(paths, pages).recovered_messages == 1


def test_recovery_holds_writer_across_http_and_releases_after_failure(tmp_path):
    paths = RecallPaths.from_root(tmp_path)

    class LockedPages(Pages):
        def post(self, url, *, json):
            response = client(paths).post(
                "/bluebubbles/webhook",
                params={"token": SECRET},
                json={"type": "new-message", "data": message("webhook")},
            )
            assert response.status_code == 503
            return super().post(url, json=json)

    with pytest.raises(RuntimeError):
        recover(paths, LockedPages([RuntimeError(SECRET)]))
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "new-message", "data": message("retry")},
    )
    assert response.status_code == 200


def test_cursor_commit_failure_retains_raw_and_retry_is_safe(tmp_path, monkeypatch):
    paths = RecallPaths.from_root(tmp_path)
    advance = capture.advance_bluebubbles_cursor

    def failed(*args, **kwargs):
        raise RuntimeError(f"cursor failure {SECRET}")

    monkeypatch.setattr(capture, "advance_bluebubbles_cursor", failed)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "new-message", "data": message()},
    )
    assert response.status_code == 503 and SECRET not in response.text
    assert cursor(paths) is None
    monkeypatch.setattr(capture, "advance_bluebubbles_cursor", advance)
    result = recover(paths, Pages([{"data": [message()]}]))
    assert result.skipped_existing == 1 and result.recovered_messages == 0
    assert cursor(paths) is not None


@pytest.mark.parametrize(
    "payload", [{"data": []}, {"data": {"messages": []}}, {"data": {"items": [message()]}}]
)
def test_supported_empty_and_nested_recovery_pages(tmp_path, payload):
    paths = RecallPaths.from_root(tmp_path)
    result = recover(paths, Pages([payload]))
    assert result.pages_fetched == 1
    expected = 1 if isinstance(payload["data"], dict) and "items" in payload["data"] else 0
    assert result.recovered_messages == expected


def test_process_writer_contention(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    paths.ensure_directories()
    lock = paths.state / "bluebubbles-capture.lock"
    script = (
        "import fcntl,sys; f=open(sys.argv[1],'a+'); "
        "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None and process.stdin is not None
        try:
            assert process.stdout.readline().strip() == "locked"
            with pytest.raises(RuntimeError, match="writer is busy"):
                recover(paths, Pages([]))
        finally:
            process.stdin.write("release\n")
            process.stdin.flush()
            process.wait(timeout=5)
    assert recover(paths, Pages([{"data": []}])).recovered_messages == 0


def test_partial_write_and_invalid_second_page_leave_cursor_unchanged(tmp_path, monkeypatch):
    paths = RecallPaths.from_root(tmp_path)

    def partial(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write('{"partial":')
        raise OSError("injected interrupted write")

    monkeypatch.setattr(capture, "write_jsonl", partial)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "new-message", "data": message()},
    )
    assert response.status_code == 503 and cursor(paths) is None
    with pytest.raises(RuntimeError):
        recover(paths, Pages([{"data": [message()]}]))
    assert cursor(paths) is None
    assert [p.read_text() for p in paths.raw.rglob("events.jsonl")] == ['{"partial":']


def test_malformed_later_page_does_not_certify_partial_recovery(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    with pytest.raises(RuntimeError):
        recover(paths, Pages([{"data": [message()]}, {"data": {}}]), page_size=1)
    assert cursor(paths) is None
    result = recover(paths, Pages([{"data": [message()]}]))
    assert result.skipped_existing == 1 and cursor(paths) is not None


@pytest.mark.parametrize("chat_shape", ["chat", "chats", "flat"])
def test_native_webhook_and_rest_have_same_chat_and_identities(tmp_path, chat_shape):
    paths = RecallPaths.from_root(tmp_path / "webhook")
    restored = RecallPaths.from_root(tmp_path / "rest")
    data = message()
    data.pop("chatGuid")
    data["handle"] = {"address": "sender@example.test", "originalROWID": 15}
    data["participants"] = [{"address": "sender@example.test"}]
    chat = {
        "guid": "fixture-chat",
        "displayName": "Fixture group",
        "participants": [{"address": "other@example.test"}],
    }
    if chat_shape == "chats":
        data["chats"] = [chat]
    elif chat_shape == "chat":
        data["chat"] = chat
    else:
        data.update(
            chatGuid=chat["guid"],
            chatDisplayName=chat["displayName"],
            handle="sender@example.test",
            participants=["sender@example.test", "other@example.test"],
        )
    assert (
        client(paths)
        .post("/bluebubbles/webhook?token=" + SECRET, json={"type": "new-message", "data": data})
        .status_code
        == 200
    )
    # Receipt day is current, not the fixture message day. Normalize its retained partition.
    raw = next(paths.raw.rglob("events.jsonl"))
    day = raw.parent.name
    before = raw.read_bytes()
    first = read_jsonl(normalize_bluebubbles_day(paths, date=day)[0])[0]
    sync_bluebubbles_entities(paths, date=day)
    import sqlite3

    with sqlite3.connect(paths.state / "recall.sqlite3") as connection:
        values = {row[0] for row in connection.execute("SELECT value FROM identities")}
    assert values == {"sender@example.test", "other@example.test"}
    assert raw.read_bytes() == before
    recover(restored, Pages([{"data": [data]}]))
    second = read_jsonl(normalize_bluebubbles_day(restored, date="2026-03-31")[0])[0]
    for event in (first, second):
        assert event["conversation_id"] == "fixture-chat"
        assert event["conversation_label"] == "Fixture group"
        assert event["sender_identity_id"] == _identity_id("sender@example.test")
        assert set(event["participant_identity_ids"]) == {
            _identity_id("sender@example.test"),
            _identity_id("other@example.test"),
        }
    assert first["event_id"] == second["event_id"]


def test_unrecognized_handle_objects_do_not_invent_identities(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    data = {
        **message(),
        "handle": {"originalROWID": 15},
        "participants": [{"displayName": "Unknown person"}, 17],
    }
    assert (
        client(paths)
        .post("/bluebubbles/webhook?token=" + SECRET, json={"type": "new-message", "data": data})
        .status_code
        == 200
    )
    raw = next(paths.raw.rglob("events.jsonl"))
    day = raw.parent.name
    before = raw.read_bytes()
    event = read_jsonl(normalize_bluebubbles_day(paths, date=day)[0])[0]
    assert event["sender_identity_id"] is None and event["participant_identity_ids"] == []
    assert sync_bluebubbles_entities(paths, date=day).identities_synced == 0
    assert raw.read_bytes() == before


def test_recovery_query_uses_millisecond_bounds(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    pages = Pages([{"data": []}])
    recover(paths, pages)
    assert pages.calls[0]["after"] == 1774915200000
    assert pages.calls[0]["before"] == 1775001600000


def test_unknown_webhook_event_is_retained_without_message_cursor(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    response = client(paths).post(
        "/bluebubbles/webhook",
        params={"token": SECRET},
        json={"type": "server-status", "data": {"state": "fixture"}},
    )
    assert response.status_code == 200 and cursor(paths) is None
    rows = [r for p in paths.raw.rglob("events.jsonl") for r in read_jsonl(p)]
    assert rows[0]["payload"] == {"type": "server-status", "data": {"state": "fixture"}}
