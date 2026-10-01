from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

from recall.connectors.telegram.artifacts import download_telegram_artifacts
from recall.connectors.telegram.capture import (
    append_telegram_update,
    capture_telegram_updates,
    load_update_payload,
)
from recall.connectors.telegram.client import FileTelegramClient
from recall.connectors.telegram.config import TelegramSourceConfig
from recall.connectors.telegram.entities import sync_telegram_entities
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.connectors.telegram.tdlib import (
    TdlibAuthSettings,
    TdlibJsonTransport,
    TdlibTelegramClient,
    build_tdlib_auth_settings,
)
from recall.storage.paths import RecallPaths

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telegram"


class FakeTdlibTransport:
    def __init__(
        self, responses: list[dict], *, request_responses: dict[tuple[str, int], dict] | None = None
    ):
        self.responses = list(responses)
        self.request_responses = request_responses or {}
        self.sent: list[dict] = []
        self.executed: list[dict] = []
        self.closed = False

    def send(self, query: dict) -> None:
        self.sent.append(query)
        key = (str(query.get("@type")), int(query.get("chat_id") or query.get("user_id") or 0))
        if key in self.request_responses:
            self.responses.append({**self.request_responses[key], "@extra": query["@extra"]})

    def receive(self, timeout: float) -> dict | None:
        del timeout
        if not self.responses:
            return None
        return self.responses.pop(0)

    def execute(self, query: dict) -> dict | None:
        self.executed.append(query)
        return {"@type": "error", "code": 400, "message": "Can't execute synchronously"}

    def close(self) -> None:
        self.closed = True


class FakeTelegramArtifactClient:
    def __init__(self, path_by_file_id: dict[int, Path]) -> None:
        self.path_by_file_id = path_by_file_id
        self.requests: list[int] = []
        self.remote_requests: list[tuple[str, str]] = []

    def download_file(
        self, file_id: int, *, timeout_seconds: float = 120.0
    ) -> dict[str, object] | None:
        del timeout_seconds
        self.requests.append(file_id)
        path = self.path_by_file_id.get(file_id)
        if path is None:
            return None
        return {"id": file_id, "local": {"path": str(path), "is_downloading_completed": True}}

    def close(self) -> None:
        return None

    def download_remote_file(
        self,
        remote_id: str,
        *,
        kind: str,
        timeout_seconds: float = 120.0,
    ) -> dict[str, object] | None:
        del timeout_seconds
        self.remote_requests.append((remote_id, kind))
        return None


def _tdlib_settings(tmp_path: Path) -> TdlibAuthSettings:
    return TdlibAuthSettings(
        account="personal",
        api_id=123,
        api_hash="hash",
        phone_number="+15551234567",
        database_directory=tmp_path / "tdlib" / "db",
        files_directory=tmp_path / "tdlib" / "files",
        code="12345",
        password="secret",
    )


def copy_fixture_capture(tmp_path: Path) -> RecallPaths:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-03-31")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "updates.jsonl", target_dir / "updates.jsonl")
    return paths


def copy_telegram_export_fixture(tmp_path: Path) -> Path:
    export_root = tmp_path / "telegram-export"
    shutil.copytree(Path(__file__).parent / "fixtures" / "telegram_export", export_root)
    return export_root


def test_normalize_telegram_day_writes_events_and_artifacts(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    event_path, artifact_path = normalize_telegram_day(paths, date="2026-03-31")

    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifact_records = [
        json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(records) == 2
    assert len(artifact_records) == 1

    first = records[0]
    assert first["source"] == "telegram"
    assert first["conversation_id"] == "1001"
    assert first["conversation_label"] == "Ariel"
    assert first["sender_identity_id"] == "ident_telegram_user_42"
    assert first["participant_identity_ids"] == [
        "ident_telegram_user_42",
        "ident_telegram_user_99",
    ]
    assert first["text"] == "Hey from Telegram https://example.com/story"
    assert first["source_urls"] == ["https://example.com/story"]
    assert first["raw_ref"] == {
        "source": "telegram",
        "path": "data/raw/telegram/2026-03-31/updates.jsonl",
        "locator": {"line": 1, "message_id": 9001, "chat_id": 1001},
    }
    assert first["tags"] == ["message", "telegram", "text"]

    second = records[1]
    assert second["sender_identity_id"] == "ident_telegram_user_99"
    assert second["source_urls"] == ["https://example.com/photo"]
    assert len(second["artifact_ids"]) == 1
    assert second["tags"] == ["message", "telegram", "photo"]

    artifact = artifact_records[0]
    assert artifact["source"] == "telegram"
    assert artifact["kind"] == "photo"
    assert artifact["source_object_id"] == "photo_001"
    assert artifact["filename"] == "IMG_1002.jpg"
    assert artifact["mime_type"] == "image/jpeg"
    assert artifact["size_bytes"] == 654321
    assert artifact["download_status"] == "not_requested"
    assert artifact["remote_locators"] == [
        {
            "kind": "local_path",
            "value": "/Users/jon/.local/share/telegram/photos/IMG_1002.jpg",
        },
        {"kind": "remote_id", "value": "telegram-file-001"},
        {"kind": "remote_unique_id", "value": "telegram-unique-001"},
    ]


def test_normalize_telegram_private_chat_uses_user_label_and_participants(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-02")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "updates.jsonl").write_text(
        json.dumps(
            {
                "account": "personal",
                "capture_mode": "fixture",
                "payload": {
                    "chat": {"id": 1002, "type": {"@type": "chatTypePrivate", "user_id": 42}},
                    "message": {
                        "chat_id": 1002,
                        "content": {"@type": "messageText", "text": {"text": "hello"}},
                        "date": 1774976467,
                        "id": 9105,
                        "sender_id": {"@type": "messageSenderUser", "user_id": 99},
                    },
                    "users": [
                        {"id": 42, "first_name": "Ariel", "last_name": "Example"},
                        {"id": 99, "first_name": "Jon", "last_name": "Suderman"},
                    ],
                },
                "received_at": "2026-04-02T17:31:08Z",
                "source": "telegram",
                "update_type": "updateNewMessage",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    event_path, _ = normalize_telegram_day(paths, date="2026-04-02")

    record = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["conversation_label"] == "Ariel Example"
    assert record["participant_identity_ids"] == [
        "ident_telegram_user_42",
        "ident_telegram_user_99",
    ]


def test_normalize_telegram_reply_forward_and_album_threading(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-03")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "updates.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "account": "personal",
                        "payload": {
                            "chat": {"id": 1003, "title": "Thread Test"},
                            "message": {
                                "chat_id": 1003,
                                "content": {"@type": "messageText", "text": {"text": "reply text"}},
                                "date": 1774976467,
                                "id": 9201,
                                "reply_to_message_id": 9100,
                                "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                            },
                        },
                        "received_at": "2026-04-03T10:00:00Z",
                        "source": "telegram",
                        "update_type": "updateNewMessage",
                    }
                ),
                json.dumps(
                    {
                        "account": "personal",
                        "payload": {
                            "chat": {"id": 1003, "title": "Thread Test"},
                            "message": {
                                "chat_id": 1003,
                                "content": {
                                    "@type": "messageText",
                                    "text": {"text": "forwarded text"},
                                },
                                "date": 1774976468,
                                "forward_info": {"origin": {"@type": "messageForwardOriginUser"}},
                                "id": 9202,
                                "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                            },
                        },
                        "received_at": "2026-04-03T10:00:01Z",
                        "source": "telegram",
                        "update_type": "updateNewMessage",
                    }
                ),
                json.dumps(
                    {
                        "account": "personal",
                        "payload": {
                            "chat": {"id": 1003, "title": "Thread Test"},
                            "message": {
                                "chat_id": 1003,
                                "content": {
                                    "@type": "messagePhoto",
                                    "caption": {"text": "album photo"},
                                },
                                "date": 1774976469,
                                "id": 9203,
                                "media_album_id": 777,
                                "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                            },
                        },
                        "received_at": "2026-04-03T10:00:02Z",
                        "source": "telegram",
                        "update_type": "updateNewMessage",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    event_path, _ = normalize_telegram_day(paths, date="2026-04-03")
    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]

    assert records[0]["thread_id"] == "reply:9100"
    assert "reply" in records[0]["tags"]
    assert records[1]["thread_id"] is None
    assert "forwarded" in records[1]["tags"]
    assert records[2]["thread_id"] == "album:777"
    assert "album" in records[2]["tags"]


def test_normalize_telegram_tdlib_photo_uses_nested_file_metadata(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-05")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "updates.jsonl").write_text(
        json.dumps(
            {
                "account": "personal",
                "payload": {
                    "message": {
                        "chat_id": 1004,
                        "content": {
                            "@type": "messagePhoto",
                            "caption": {"text": ""},
                            "photo": {
                                "sizes": [
                                    {
                                        "width": 320,
                                        "height": 240,
                                        "photo": {
                                            "id": 1256,
                                            "expected_size": 23529,
                                            "local": {
                                                "path": "/tmp/telegram-small.jpg",
                                                "is_downloading_completed": False,
                                            },
                                            "remote": {
                                                "id": "remote-small",
                                                "unique_id": "unique-small",
                                            },
                                        },
                                    },
                                    {
                                        "width": 1280,
                                        "height": 960,
                                        "photo": {
                                            "id": 1259,
                                            "expected_size": 28741,
                                            "local": {
                                                "path": "/tmp/telegram-large.jpg",
                                                "is_downloading_completed": False,
                                            },
                                            "remote": {
                                                "id": "remote-large",
                                                "unique_id": "unique-large",
                                            },
                                        },
                                    },
                                ]
                            },
                        },
                        "date": 1774976467,
                        "id": 9301,
                        "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                    }
                },
                "received_at": "2026-04-05T10:00:00Z",
                "source": "telegram",
                "update_type": "updateNewMessage",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, artifact_path = normalize_telegram_day(paths, date="2026-04-05")

    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert artifact["source_object_id"] == "1259"
    assert artifact["size_bytes"] == 28741
    assert artifact["remote_locators"] == [
        {"kind": "local_path", "value": "/tmp/telegram-large.jpg"},
        {"kind": "remote_id", "value": "remote-large"},
        {"kind": "remote_unique_id", "value": "unique-large"},
    ]


def test_normalize_telegram_tdlib_document_zip_uses_nested_file_metadata(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-05")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "updates.jsonl").write_text(
        json.dumps(
            {
                "account": "personal",
                "payload": {
                    "message": {
                        "chat_id": 1004,
                        "content": {
                            "@type": "messageDocument",
                            "caption": {"text": "zip attachment"},
                            "document": {
                                "file_name": "archive.zip",
                                "mime_type": "application/zip",
                                "document": {
                                    "id": 1301,
                                    "expected_size": 54321,
                                    "local": {
                                        "path": "/tmp/archive.zip",
                                        "is_downloading_completed": False,
                                    },
                                    "remote": {
                                        "id": "remote-zip",
                                        "unique_id": "unique-zip",
                                    },
                                },
                            },
                        },
                        "date": 1774976467,
                        "id": 9302,
                        "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                    }
                },
                "received_at": "2026-04-05T10:00:01Z",
                "source": "telegram",
                "update_type": "updateNewMessage",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    event_path, artifact_path = normalize_telegram_day(paths, date="2026-04-05")

    event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["text"] == "zip attachment"
    assert "document" in event["tags"]
    assert artifact["kind"] == "document"
    assert artifact["source_object_id"] == "1301"
    assert artifact["filename"] == "archive.zip"
    assert artifact["mime_type"] == "application/zip"
    assert artifact["size_bytes"] == 54321
    assert artifact["remote_locators"] == [
        {"kind": "local_path", "value": "/tmp/archive.zip"},
        {"kind": "remote_id", "value": "remote-zip"},
        {"kind": "remote_unique_id", "value": "unique-zip"},
    ]


def test_normalize_telegram_supports_video_audio_sticker_and_video_note(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-06")
    target_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "account": "personal",
            "payload": {
                "message": {
                    "chat_id": 1005,
                    "content": {
                        "@type": "messageVideo",
                        "caption": {"text": "video caption"},
                        "video": {
                            "file_name": "clip.mp4",
                            "mime_type": "video/mp4",
                            "video": {
                                "id": 2001,
                                "expected_size": 111,
                                "local": {"path": "/tmp/clip.mp4"},
                                "remote": {"id": "remote-video", "unique_id": "unique-video"},
                            },
                        },
                    },
                    "date": 1774976467,
                    "id": 9401,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                }
            },
            "received_at": "2026-04-06T10:00:00Z",
            "source": "telegram",
            "update_type": "updateNewMessage",
        },
        {
            "account": "personal",
            "payload": {
                "message": {
                    "chat_id": 1005,
                    "content": {
                        "@type": "messageAudio",
                        "caption": {"text": "audio caption"},
                        "audio": {
                            "file_name": "song.mp3",
                            "mime_type": "audio/mpeg",
                            "audio": {
                                "id": 2002,
                                "expected_size": 222,
                                "local": {"path": "/tmp/song.mp3"},
                                "remote": {"id": "remote-audio", "unique_id": "unique-audio"},
                            },
                        },
                    },
                    "date": 1774976468,
                    "id": 9402,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                }
            },
            "received_at": "2026-04-06T10:00:01Z",
            "source": "telegram",
            "update_type": "updateNewMessage",
        },
        {
            "account": "personal",
            "payload": {
                "message": {
                    "chat_id": 1005,
                    "content": {
                        "@type": "messageSticker",
                        "sticker": {
                            "emoji": "🙂",
                            "set_name": "funny_pack",
                            "format": {"@type": "stickerFormatWebp"},
                            "sticker": {
                                "id": 2003,
                                "expected_size": 333,
                                "local": {"path": "/tmp/sticker.webp"},
                                "remote": {"id": "remote-sticker", "unique_id": "unique-sticker"},
                            },
                        },
                    },
                    "date": 1774976469,
                    "id": 9403,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                }
            },
            "received_at": "2026-04-06T10:00:02Z",
            "source": "telegram",
            "update_type": "updateNewMessage",
        },
        {
            "account": "personal",
            "payload": {
                "message": {
                    "chat_id": 1005,
                    "content": {
                        "@type": "messageVideoNote",
                        "caption": {"text": ""},
                        "video_note": {
                            "video": {
                                "id": 2004,
                                "expected_size": 444,
                                "local": {"path": "/tmp/video-note.mp4"},
                                "remote": {
                                    "id": "remote-video-note",
                                    "unique_id": "unique-video-note",
                                },
                            }
                        },
                    },
                    "date": 1774976470,
                    "id": 9404,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                }
            },
            "received_at": "2026-04-06T10:00:03Z",
            "source": "telegram",
            "update_type": "updateNewMessage",
        },
    ]
    (target_dir / "updates.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    event_path, artifact_path = normalize_telegram_day(paths, date="2026-04-06")
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifacts = [
        json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [artifact["kind"] for artifact in artifacts] == [
        "video",
        "audio",
        "sticker",
        "video_note",
    ]
    assert artifacts[0]["filename"] == "clip.mp4"
    assert artifacts[1]["mime_type"] == "audio/mpeg"
    assert artifacts[2]["filename"] == "funny_pack"
    assert artifacts[2]["mime_type"] == "stickerFormatWebp"
    assert artifacts[3]["source_object_id"] == "2004"
    assert "video" in events[0]["tags"]
    assert "audio" in events[1]["tags"]
    assert events[2]["text"] == "🙂"
    assert "sticker" in events[2]["tags"]
    assert "video_note" in events[3]["tags"]


def test_sync_telegram_entities_persists_users_chats_and_aliases(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    result = sync_telegram_entities(paths, date="2026-03-31")

    assert result.persons_synced == 2
    assert result.identities_synced == 6
    assert result.person_aliases_synced == 5
    assert result.aliases_synced == 9
    assert result.resolutions_synced == 5

    with sqlite3.connect(paths.database) as connection:
        persons = connection.execute(
            "select person_id, display_name from persons order by person_id"
        ).fetchall()
        identities = connection.execute(
            "select source, kind, value, person_id from identities order by kind, value"
        ).fetchall()
        person_aliases = connection.execute(
            "select person_id, value, source from aliases order by person_id, value, source"
        ).fetchall()
        aliases = connection.execute(
            (
                "select identity_id, value, source from identity_aliases "
                "order by identity_id, value, source"
            )
        ).fetchall()
        resolutions = connection.execute(
            "select identity_id, person_id, method from resolutions order by identity_id, method"
        ).fetchall()

    assert persons == [
        ("person_telegram_user_42", "Ariel Example"),
        ("person_telegram_user_99", "Jon Suderman"),
    ]
    assert identities == [
        ("telegram", "chat_id", "1001", None),
        ("telegram", "phone_number", "+15551234567", "person_telegram_user_42"),
        ("telegram", "user_id", "42", "person_telegram_user_42"),
        ("telegram", "user_id", "99", "person_telegram_user_99"),
        ("telegram", "username", "ariel", "person_telegram_user_42"),
        ("telegram", "username", "jonsuderman", "person_telegram_user_99"),
    ]
    assert len(person_aliases) == 5
    assert {value for _, value, _ in person_aliases} == {
        "+15551234567",
        "@ariel",
        "@jonsuderman",
        "Ariel Example",
        "Jon Suderman",
    }
    assert len(aliases) == 9
    assert {value for _, value, _ in aliases} == {
        "+15551234567",
        "@ariel",
        "@jonsuderman",
        "Ariel",
        "Ariel Example",
        "Jon Suderman",
    }
    assert len(resolutions) == 5
    assert {method for _, _, method in resolutions} == {
        "telegram_phone_number",
        "telegram_user_id",
        "telegram_username",
    }


def test_sync_telegram_entities_is_idempotent(tmp_path) -> None:
    paths = copy_fixture_capture(tmp_path)

    first = sync_telegram_entities(paths, date="2026-03-31")
    second = sync_telegram_entities(paths, date="2026-03-31")

    assert first.persons_synced == second.persons_synced == 2
    assert first.identities_synced == second.identities_synced == 6
    assert first.person_aliases_synced == second.person_aliases_synced == 5
    assert first.aliases_synced == second.aliases_synced == 9
    assert first.resolutions_synced == second.resolutions_synced == 5

    with sqlite3.connect(paths.database) as connection:
        person_count = connection.execute("select count(*) from persons").fetchone()[0]
        identity_count = connection.execute("select count(*) from identities").fetchone()[0]
        person_alias_count = connection.execute("select count(*) from aliases").fetchone()[0]
        alias_count = connection.execute("select count(*) from identity_aliases").fetchone()[0]
        resolution_count = connection.execute("select count(*) from resolutions").fetchone()[0]

    assert person_count == 2
    assert identity_count == 6
    assert person_alias_count == 5
    assert alias_count == 9
    assert resolution_count == 5


def test_sync_telegram_entities_resolves_private_chat_identity_to_person(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    target_dir = paths.raw_capture_dir("telegram", "2026-04-04")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "updates.jsonl").write_text(
        json.dumps(
            {
                "account": "personal",
                "payload": {
                    "chat": {
                        "id": 1002,
                        "type": {"@type": "chatTypePrivate", "user_id": 42},
                    },
                    "message": {
                        "chat_id": 1002,
                        "content": {"@type": "messageText", "text": {"text": "hello"}},
                        "date": 1774976467,
                        "id": 9106,
                        "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                    },
                    "users": [
                        {
                            "id": 42,
                            "first_name": "Ariel",
                            "last_name": "Example",
                            "usernames": {"active_usernames": ["ariel"]},
                        }
                    ],
                },
                "received_at": "2026-04-04T10:00:00Z",
                "source": "telegram",
                "update_type": "updateNewMessage",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = sync_telegram_entities(paths, date="2026-04-04")

    assert result.persons_synced == 1
    assert result.resolutions_synced == 3
    with sqlite3.connect(paths.database) as connection:
        chat_identity = connection.execute(
            "select person_id from identities where source = 'telegram' "
            "and kind = 'chat_id' and value = '1002'"
        ).fetchone()
        chat_resolution = connection.execute(
            "select method from resolutions where identity_id = 'ident_telegram_chat_1002'"
        ).fetchone()

    assert chat_identity == ("person_telegram_user_42",)
    assert chat_resolution == ("telegram_private_chat",)


def test_append_telegram_update_writes_raw_envelope_and_cursor(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    payload = load_update_payload(FIXTURE_DIR / "update.json")

    result = append_telegram_update(
        paths,
        account="personal",
        payload=payload,
        update_type="updateNewMessage",
        update_id=12345,
        received_at="2026-03-31T18:00:00Z",
    )

    row = json.loads(result.updates_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["source"] == "telegram"
    assert row["capture_mode"] == "manual"
    assert row["update_type"] == "updateNewMessage"
    assert row["update_id"] == 12345
    assert row["payload"]["message"]["id"] == 9003
    assert result.cursor is not None
    assert result.cursor.cursor_key == "last_update_id"
    assert result.cursor.cursor_value == "12345"


def test_download_telegram_artifacts_copies_existing_local_media(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    source_file = tmp_path / "source-photo.jpg"
    source_file.write_bytes(b"telegram-photo")
    artifact_path = paths.artifact_metadata_path("telegram", "2026-04-02")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_id": "artifact_test",
                "source": "telegram",
                "kind": "photo",
                "account": "personal",
                "source_object_id": "9001",
                "event_ids": ["evt_test"],
                "remote_locators": [{"kind": "local_path", "value": str(source_file)}],
                "local_path": None,
                "mime_type": "image/jpeg",
                "filename": "photo.jpg",
                "size_bytes": 14,
                "checksums": {},
                "download_status": "not_requested",
                "last_error": None,
                "observed_at": "2026-04-02T10:00:00Z",
                "raw_ref": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = download_telegram_artifacts(
        paths,
        date="2026-04-02",
        policy="download-source-native",
    )

    assert result.downloaded == 1
    records = [json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["download_status"] == "downloaded"
    blob_path = tmp_path / records[0]["local_path"]
    assert blob_path.read_bytes() == b"telegram-photo"


def test_download_telegram_artifacts_uses_tdlib_when_local_file_missing(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    downloaded_file = tmp_path / "tdlib-photo.jpg"
    downloaded_file.write_bytes(b"tdlib-photo")
    artifact_path = paths.artifact_metadata_path("telegram", "2026-04-02")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_id": "artifact_test",
                "source": "telegram",
                "kind": "photo",
                "account": "personal",
                "source_object_id": "321",
                "event_ids": ["evt_test"],
                "remote_locators": [{"kind": "local_path", "value": str(tmp_path / "missing.jpg")}],
                "local_path": None,
                "mime_type": "image/jpeg",
                "filename": "photo.jpg",
                "size_bytes": 11,
                "checksums": {},
                "download_status": "not_requested",
                "last_error": None,
                "observed_at": "2026-04-02T10:00:00Z",
                "raw_ref": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    client = FakeTelegramArtifactClient({321: downloaded_file})
    result = download_telegram_artifacts(
        paths,
        date="2026-04-02",
        policy="download-source-native",
        client=client,
    )

    assert result.downloaded == 1
    assert client.requests == [321]


def test_download_telegram_artifacts_uses_remote_id_fallback(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    downloaded_file = tmp_path / "tdlib-remote-photo.jpg"
    downloaded_file.write_bytes(b"tdlib-remote-photo")
    artifact_path = paths.artifact_metadata_path("telegram", "2026-04-02")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_id": "artifact_test",
                "source": "telegram",
                "kind": "photo",
                "account": "personal",
                "source_object_id": "1258",
                "event_ids": ["evt_test"],
                "remote_locators": [
                    {"kind": "remote_id", "value": "remote-photo-123"},
                    {"kind": "remote_unique_id", "value": "unique-photo-123"},
                ],
                "local_path": None,
                "mime_type": "image/jpeg",
                "filename": "photo.jpg",
                "size_bytes": 17,
                "checksums": {},
                "download_status": "not_requested",
                "last_error": None,
                "observed_at": "2026-04-02T10:00:00Z",
                "raw_ref": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class RemoteFallbackClient(FakeTelegramArtifactClient):
        def download_file(
            self, file_id: int, *, timeout_seconds: float = 120.0
        ) -> dict[str, object] | None:
            del timeout_seconds
            self.requests.append(file_id)
            raise RuntimeError("File not found")

        def download_remote_file(
            self,
            remote_id: str,
            *,
            kind: str,
            timeout_seconds: float = 120.0,
        ) -> dict[str, object] | None:
            del timeout_seconds
            self.remote_requests.append((remote_id, kind))
            return {
                "id": 9000,
                "local": {"path": str(downloaded_file), "is_downloading_completed": True},
            }

    client = RemoteFallbackClient({})
    result = download_telegram_artifacts(
        paths,
        date="2026-04-02",
        policy="download-source-native",
        client=client,
    )

    assert result.downloaded == 1
    assert client.requests == [1258]
    assert client.remote_requests == [("remote-photo-123", "photo")]


def test_download_telegram_artifacts_dry_run_does_not_call_tdlib(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    artifact_path = paths.artifact_metadata_path("telegram", "2026-04-02")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_id": "artifact_test",
                "source": "telegram",
                "kind": "photo",
                "account": "personal",
                "source_object_id": "1258",
                "event_ids": ["evt_test"],
                "remote_locators": [{"kind": "remote_id", "value": "remote-photo-123"}],
                "local_path": None,
                "mime_type": "image/jpeg",
                "filename": "photo.jpg",
                "size_bytes": 17,
                "checksums": {},
                "download_status": "not_requested",
                "last_error": None,
                "observed_at": "2026-04-02T10:00:00Z",
                "raw_ref": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class FailingClient(FakeTelegramArtifactClient):
        def download_file(
            self, file_id: int, *, timeout_seconds: float = 120.0
        ) -> dict[str, object] | None:
            raise AssertionError("dry run should not call download_file")

        def download_remote_file(
            self,
            remote_id: str,
            *,
            kind: str,
            timeout_seconds: float = 120.0,
        ) -> dict[str, object] | None:
            raise AssertionError("dry run should not call download_remote_file")

    client = FailingClient({})
    result = download_telegram_artifacts(
        paths,
        date="2026-04-02",
        policy="download-source-native",
        client=client,
        dry_run=True,
    )

    assert result.would_download == 1
    assert result.failed == 0


def test_import_telegram_export_directory_writes_import_and_raw_updates(tmp_path) -> None:
    from recall.connectors.telegram.importer import import_telegram_export

    paths = RecallPaths.from_root(tmp_path)
    export_root = copy_telegram_export_fixture(tmp_path)

    result = import_telegram_export(paths, export_path=export_root, account="personal")

    assert result.messages_imported == 2
    assert result.dates_written == ["2026-04-02"]
    assert (result.import_dir / "result.json").exists()
    assert (result.import_dir / "files" / "archive.zip").read_text(
        encoding="utf-8"
    ) == "fake-zip-bytes\n"

    raw_updates = (paths.raw_capture_dir("telegram", "2026-04-02") / "updates.jsonl").read_text(
        encoding="utf-8"
    )
    rows = [json.loads(line) for line in raw_updates.splitlines()]
    assert len(rows) == 2
    assert rows[0]["capture_mode"] == "import"
    assert rows[1]["payload"]["message"]["content"]["@type"] == "messageDocument"
    assert rows[1]["payload"]["message"]["content"]["document"]["document"]["local"][
        "path"
    ].endswith("files/archive.zip")


def test_import_telegram_export_zip_writes_import_and_can_normalize(tmp_path) -> None:
    from recall.connectors.telegram.importer import import_telegram_export

    paths = RecallPaths.from_root(tmp_path)
    export_root = copy_telegram_export_fixture(tmp_path)
    zip_path = tmp_path / "telegram-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for file_path in export_root.rglob("*"):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(export_root))

    result = import_telegram_export(paths, export_path=zip_path, account="personal")

    assert result.messages_imported == 2
    event_path, artifact_path = normalize_telegram_day(paths, date="2026-04-02")
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifacts = [
        json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 2
    assert artifacts[0]["kind"] == "document"
    assert artifacts[0]["filename"] == "archive.zip"


def test_tdlib_client_download_remote_file_tries_unknown_then_typed(tmp_path) -> None:
    class RequestTransport:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.responses: list[dict] = []

        def send(self, query: dict) -> None:
            self.sent.append(query)
            extra = query.get("@extra")
            if query.get("@type") == "getRemoteFile" and "file_type" not in query:
                self.responses.append(
                    {"@type": "error", "@extra": extra, "message": "need file type"}
                )
            elif query.get("@type") == "getRemoteFile":
                self.responses.append({"@type": "file", "@extra": extra, "id": 42})
            elif query.get("@type") == "getFile":
                self.responses.append(
                    {
                        "@type": "file",
                        "@extra": extra,
                        "id": 42,
                        "local": {
                            "path": str(tmp_path / "remote.bin"),
                            "is_downloading_completed": True,
                        },
                    }
                )
            elif query.get("@type") == "downloadFile":
                self.responses.append(
                    {
                        "@type": "file",
                        "@extra": extra,
                        "id": 42,
                        "local": {
                            "path": str(tmp_path / "remote.bin"),
                            "is_downloading_completed": True,
                        },
                    }
                )

        def receive(self, timeout: float) -> dict | None:
            del timeout
            if not self.responses:
                return None
            return self.responses.pop(0)

        def execute(self, query: dict) -> dict | None:
            return None

        def close(self) -> None:
            return None

    transport = RequestTransport()
    client = TdlibTelegramClient(
        transport=transport,
        settings=_tdlib_settings(tmp_path),
        auth_timeout_seconds=1.0,
        receive_timeout_seconds=0.01,
    )
    client._ready = True  # type: ignore[attr-defined]

    result = client.download_remote_file("remote-photo-123", kind="photo")

    assert result is not None
    remote_queries = [query for query in transport.sent if query.get("@type") == "getRemoteFile"]
    assert [
        {key: value for key, value in query.items() if key != "@extra"} for query in remote_queries
    ] == [
        {"@type": "getRemoteFile", "remote_file_id": "remote-photo-123"},
        {
            "@type": "getRemoteFile",
            "remote_file_id": "remote-photo-123",
            "file_type": {"@type": "fileTypePhoto"},
        },
    ]


def test_tdlib_json_transport_sets_global_log_verbosity_before_client_create(monkeypatch) -> None:
    import recall.connectors.telegram.tdlib as tdlib_module

    calls: list[tuple[str, tuple]] = []

    class FakeFunction:
        def __init__(self, name: str, return_value=None):
            self.name = name
            self.return_value = return_value
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.return_value

    class FakeLibrary:
        def __init__(self) -> None:
            self.td_set_log_verbosity_level = FakeFunction("td_set_log_verbosity_level")
            self.td_json_client_create = FakeFunction("td_json_client_create", 123)
            self.td_json_client_send = FakeFunction("td_json_client_send")
            self.td_json_client_receive = FakeFunction("td_json_client_receive", None)
            self.td_json_client_execute = FakeFunction(
                "td_json_client_execute",
                b'{"@type":"ok"}',
            )
            self.td_json_client_destroy = FakeFunction("td_json_client_destroy")

    monkeypatch.setattr(tdlib_module.ctypes, "CDLL", lambda path: FakeLibrary())

    transport = TdlibJsonTransport(library_path="/tmp/libtdjson.so", log_verbosity_level=0)
    transport.close()

    assert calls[0] == ("td_set_log_verbosity_level", (0,))
    execute_calls = [entry for entry in calls if entry[0] == "td_json_client_execute"]
    assert len(execute_calls) == 2
    first_payload = execute_calls[0][1][1].decode("utf-8")
    second_payload = execute_calls[1][1][1].decode("utf-8")
    assert '"setLogStream"' in first_payload
    assert '"logStreamEmpty"' in first_payload
    assert '"setLogVerbosityLevel"' in second_payload


def test_file_telegram_client_filters_updates_after_cursor() -> None:
    client = FileTelegramClient.from_path(FIXTURE_DIR / "update_stream.jsonl")

    updates = client.get_updates(after_update_id=12345)

    assert len(updates) == 1
    assert updates[0].update_id == 12346
    assert updates[0].payload["message"]["id"] == 9102


def test_capture_telegram_updates_appends_batch_and_tracks_cursor(tmp_path) -> None:
    paths = RecallPaths.from_root(tmp_path)
    client = FileTelegramClient.from_path(FIXTURE_DIR / "update_stream.jsonl")

    result = capture_telegram_updates(
        paths,
        client=client,
        account="personal",
        after_update_id=12345,
        capture_mode="run",
    )

    assert result.captured_updates == 1
    assert result.dates_written == ["2026-03-31"]
    assert result.last_update_id == 12346
    assert result.cursor is not None
    assert result.cursor.cursor_value == "12346"

    updates_path = paths.raw_capture_dir("telegram", "2026-03-31") / "updates.jsonl"
    rows = [json.loads(line) for line in updates_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["capture_mode"] == "run"
    assert rows[0]["update_id"] == 12346


def test_build_tdlib_auth_settings_reads_env_and_state_paths(tmp_path, monkeypatch) -> None:
    paths = RecallPaths.from_root(tmp_path)
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash-value")
    monkeypatch.setenv("TELEGRAM_PHONE_NUMBER", "+15551234567")
    monkeypatch.setenv("TELEGRAM_AUTH_CODE", "99999")
    monkeypatch.setenv("TELEGRAM_AUTH_PASSWORD", "password")

    settings = build_tdlib_auth_settings(paths, TelegramSourceConfig(), account="work")

    assert settings.api_id == 123456
    assert settings.api_hash == "hash-value"
    assert settings.phone_number == "+15551234567"
    assert settings.code == "99999"
    assert settings.password == "password"
    assert settings.log_verbosity_level == 0
    assert settings.database_directory == tmp_path / "data/state/telegram/tdlib/work/database"
    assert settings.files_directory == tmp_path / "data/state/telegram/tdlib/work/files"


def test_tdlib_client_authenticates_and_enriches_update(tmp_path) -> None:
    transport = FakeTdlibTransport(
        [
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
            },
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitPhoneNumber"},
            },
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitCode"},
            },
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitPassword"},
            },
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateReady"},
            },
            {
                "@type": "updateNewMessage",
                "message": {
                    "id": 9103,
                    "chat_id": 1001,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 42},
                    "content": {"@type": "messageText", "text": {"text": "hello tdlib"}},
                },
            },
        ],
        request_responses={
            ("getChat", 1001): {"id": 1001, "title": "Ariel", "participant_user_ids": [42]},
            (
                "getUser",
                42,
            ): {"id": 42, "first_name": "Ariel", "last_name": "Example", "usernames": ["ariel"]},
        },
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=_tdlib_settings(tmp_path),
        auth_timeout_seconds=1.0,
        receive_timeout_seconds=0.01,
    )

    updates = client.get_updates(after_update_id=10, limit=1)

    assert len(updates) == 1
    update = updates[0]
    assert update.update_type == "updateNewMessage"
    assert update.update_id == 11
    assert update.payload["chat"]["title"] == "Ariel"
    assert update.payload["users"][0]["id"] == 42
    assert transport.sent[0]["@type"] == "getAuthorizationState"
    assert any(message["@type"] == "setTdlibParameters" for message in transport.sent)
    assert any(message["@type"] == "setAuthenticationPhoneNumber" for message in transport.sent)
    assert any(message["@type"] == "checkAuthenticationCode" for message in transport.sent)
    assert any(message["@type"] == "checkAuthenticationPassword" for message in transport.sent)


def test_tdlib_client_requires_code_when_tdlib_asks_for_it(tmp_path) -> None:
    transport = FakeTdlibTransport(
        [
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitCode"},
            }
        ]
    )
    settings = TdlibAuthSettings(
        account="personal",
        api_id=123,
        api_hash="hash",
        phone_number="+15551234567",
        database_directory=tmp_path / "tdlib" / "db",
        files_directory=tmp_path / "tdlib" / "files",
        code=None,
        password=None,
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=settings,
        auth_timeout_seconds=1.0,
        receive_timeout_seconds=0.01,
    )

    try:
        client.get_updates(limit=1)
    except RuntimeError as exc:
        assert "authentication code" in str(exc)
    else:
        raise AssertionError("Expected missing-code TDLib auth failure")


def test_tdlib_client_prompts_for_code_when_interactive(tmp_path) -> None:
    transport = FakeTdlibTransport(
        [
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitCode"},
            },
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateReady"},
            },
        ]
    )
    prompts: list[tuple[str, bool]] = []
    settings = TdlibAuthSettings(
        account="personal",
        api_id=123,
        api_hash="hash",
        phone_number="+15551234567",
        database_directory=tmp_path / "tdlib" / "db",
        files_directory=tmp_path / "tdlib" / "files",
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=settings,
        auth_timeout_seconds=1.0,
        receive_timeout_seconds=0.01,
        prompt_callback=lambda message, hide_input: (
            prompts.append((message, hide_input)) or "24680"
        ),
        is_interactive=True,
    )

    updates = client.get_updates(limit=1)

    assert updates == []
    assert prompts == [("Telegram sent a login code. Enter it to continue: ", False)]
    assert any(message.get("code") == "24680" for message in transport.sent)


def test_tdlib_client_enriches_private_chat_participants(tmp_path) -> None:
    transport = FakeTdlibTransport(
        [
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateReady"},
            },
            {
                "@type": "updateNewMessage",
                "message": {
                    "id": 9104,
                    "chat_id": 1002,
                    "sender_id": {"@type": "messageSenderUser", "user_id": 99},
                    "content": {"@type": "messageText", "text": {"text": "self to other"}},
                },
            },
        ],
        request_responses={
            (
                "getChat",
                1002,
            ): {"id": 1002, "type": {"@type": "chatTypePrivate", "user_id": 42}},
            ("getUser", 42): {"id": 42, "first_name": "Ariel", "last_name": "Example"},
            ("getUser", 99): {"id": 99, "first_name": "Jon", "last_name": "Suderman"},
        },
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=_tdlib_settings(tmp_path),
        auth_timeout_seconds=1.0,
        receive_timeout_seconds=0.01,
    )

    updates = client.get_updates(limit=1)

    assert updates[0].payload["chat"]["participant_user_ids"] == [42, 99]
    assert sorted(user["id"] for user in updates[0].payload["users"]) == [42, 99]
