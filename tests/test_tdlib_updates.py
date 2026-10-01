from __future__ import annotations

import itertools

import pytest
from test_telegram import FakeTdlibTransport, _tdlib_settings

from recall.connectors.telegram.tdlib import TdlibTelegramClient


def message(number: int) -> dict:
    return {
        "@type": "updateNewMessage",
        "message": {
            "id": number,
            "chat_id": 1001,
            "sender_id": {"@type": "messageSenderUser", "user_id": 42},
            "content": {"@type": "messageText", "text": {"text": f"message {number}"}},
        },
    }


def test_async_enrichment_keeps_order_timestamps_and_limits(tmp_path, monkeypatch):
    import recall.connectors.telegram.tdlib as tdlib

    ticks = itertools.count()
    monkeypatch.setattr(tdlib, "current_timestamp", lambda: f"2026-10-01T20:00:{next(ticks):02d}Z")
    file_update = {"@type": "updateFile", "file": {"id": 7}}
    transport = FakeTdlibTransport(
        [
            message(1),  # Updates received before authorization is ready must survive too.
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateReady"},
            },
            message(2),
            {"@type": "chat", "@extra": "unrelated-reply", "id": -1},
            file_update,
            message(3),
        ],
        request_responses={
            ("getChat", 1001): {"@type": "chat", "id": 1001, "title": "Ariel"},
            ("getUser", 42): {"@type": "user", "id": 42, "first_name": "Ariel"},
        },
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=_tdlib_settings(tmp_path),
        auth_timeout_seconds=1,
        receive_timeout_seconds=0.01,
    )
    first = client.get_updates(after_update_id=100, limit=1)
    second = client.get_updates(after_update_id=101, limit=1)
    rest = client.get_updates(after_update_id=102)
    updates = first + second + rest

    assert [u.update_id for u in updates] == [101, 102, 103, 104]
    assert [u.payload.get("message", {}).get("id") for u in updates] == [1, 2, None, 3]
    assert [u.received_at for u in updates] == [f"2026-10-01T20:00:{n:02d}Z" for n in range(4)]
    for update in [first[0], second[0], rest[1]]:
        assert update.payload["chat"]["title"] == "Ariel"
        assert update.payload["users"][0]["first_name"] == "Ariel"
    assert rest[0].payload == file_update
    assert [q["@type"] for q in transport.sent] == ["getAuthorizationState", "getChat", "getUser"]
    assert not transport.executed
    assert client.get_updates(after_update_id=104) == []


@pytest.mark.parametrize("reply", [None, {"@type": "error", "code": 404, "message": "Not found"}])
def test_lookup_failure_keeps_message_and_unrelated_updates(tmp_path, monkeypatch, reply):
    import recall.connectors.telegram.tdlib as tdlib

    clock = itertools.count()
    monkeypatch.setattr(tdlib.time, "monotonic", lambda: next(clock) / 10)
    transport = FakeTdlibTransport(
        [message(1), {"@type": "updateChatTitle", "chat_id": 1001, "title": "New title"}],
        request_responses={("getChat", 1001): reply} if reply is not None else {},
    )
    client = TdlibTelegramClient(
        transport=transport, settings=_tdlib_settings(tmp_path), receive_timeout_seconds=0.25
    )
    client._ready = True
    first = client.get_updates(after_update_id=10, limit=1)
    remaining = client.get_updates(after_update_id=11)
    assert first[0].payload["message"] == message(1)["message"]
    assert "chat" not in first[0].payload
    assert [u.update_type for u in remaining] == ["updateChatTitle"]
    assert remaining[0].update_id == 12
    assert not transport.executed


@pytest.mark.parametrize("operation", ["lookup", "download"])
def test_file_requests_keep_all_interleaved_updates(tmp_path, operation):
    ready_file = {
        "@type": "file",
        "id": 7,
        "local": {"path": str(tmp_path / "photo.jpg"), "is_downloading_completed": True},
    }
    file_update = {"@type": "updateFile", "file": ready_file}
    transport = FakeTdlibTransport(
        [message(1), file_update, {"@type": "updateChatTitle", "chat_id": 1001, "title": "Title"}],
        request_responses={
            ("getFile", 0): ready_file if operation == "lookup" else {"@type": "file", "id": 7},
            ("downloadFile", 0): ready_file,
            ("getChat", 1001): {"@type": "chat", "id": 1001, "title": "Ariel"},
            ("getUser", 42): {"@type": "user", "id": 42, "first_name": "Ariel"},
        },
    )
    client = TdlibTelegramClient(
        transport=transport, settings=_tdlib_settings(tmp_path), receive_timeout_seconds=0.01
    )
    client._ready = True
    result = client.download_file(7, timeout_seconds=1)
    assert result is not None and result["local"]["is_downloading_completed"]
    updates = client.get_updates(after_update_id=20)
    assert [u.update_type for u in updates] == ["updateNewMessage", "updateFile", "updateChatTitle"]
    assert [u.update_id for u in updates] == [21, 22, 23]
    assert updates[0].payload["message"] == message(1)["message"]
    assert updates[1].payload == file_update


def test_channel_sender_keeps_chat_label_without_inventing_user(tmp_path):
    update = message(1)
    update["message"]["sender_id"] = {"@type": "messageSenderChat", "chat_id": 1001}
    transport = FakeTdlibTransport(
        [update],
        request_responses={
            ("getChat", 1001): {
                "@type": "chat",
                "id": 1001,
                "title": "Channel",
                "type": {"@type": "chatTypeSupergroup", "is_channel": True},
            },
        },
    )
    client = TdlibTelegramClient(
        transport=transport, settings=_tdlib_settings(tmp_path), receive_timeout_seconds=0.01
    )
    client._ready = True
    updates = client.get_updates(limit=1)
    assert updates[0].payload["chat"]["title"] == "Channel"
    assert "users" not in updates[0].payload
    assert [q["@type"] for q in transport.sent] == ["getChat"]
    assert not transport.executed


def test_download_error_keeps_interleaved_updates(tmp_path):
    transport = FakeTdlibTransport(
        [],
        request_responses={
            ("getFile", 0): {"@type": "file", "id": 7},
            ("downloadFile", 0): {"@type": "error", "code": 400, "message": "Download failed"},
            ("getChat", 1001): {"@type": "chat", "id": 1001},
            ("getUser", 42): {"@type": "user", "id": 42},
        },
    )
    send = transport.send

    def interleave(query):
        if query["@type"] == "downloadFile":
            transport.responses.append(message(1))
        send(query)

    transport.send = interleave
    client = TdlibTelegramClient(
        transport=transport, settings=_tdlib_settings(tmp_path), receive_timeout_seconds=0.01
    )
    client._ready = True
    with pytest.raises(RuntimeError, match="Download failed"):
        client.download_file(7, timeout_seconds=1)
    updates = client.get_updates(after_update_id=30)
    assert len(updates) == 1 and updates[0].update_id == 31
    assert updates[0].payload["message"] == message(1)["message"]
