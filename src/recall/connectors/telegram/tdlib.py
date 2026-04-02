from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes.util import find_library
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from recall.connectors.telegram.client import TelegramCaptureClient, TelegramUpdate
from recall.connectors.telegram.config import (
    TelegramSourceConfig,
    read_config_env,
    resolve_tdlib_state_dir,
)
from recall.connectors.telegram.capture import current_timestamp
from recall.storage.paths import RecallPaths

AUTH_TIMEOUT_SECONDS = 30.0
DEFAULT_RECEIVE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class TdlibAuthSettings:
    account: str
    api_id: int
    api_hash: str
    phone_number: str
    database_directory: Path
    files_directory: Path
    library_path: str | None = None
    code: str | None = None
    password: str | None = None
    log_verbosity_level: int = 0
    use_test_dc: bool = False
    system_language_code: str = "en"
    device_model: str = "Recall"
    system_version: str = "Linux"
    application_version: str = "0.1.0"


class TdlibTransport(Protocol):
    def send(self, query: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def execute(self, query: dict[str, Any]) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


def build_tdlib_auth_settings(
    paths: RecallPaths,
    config: TelegramSourceConfig,
    *,
    account: str | None = None,
) -> TdlibAuthSettings:
    resolved_account = account or config.account
    api_id = read_config_env(config.api_id_env_var)
    api_hash = read_config_env(config.api_hash_env_var)
    phone_number = read_config_env(config.phone_number_env_var)

    missing: list[str] = []
    if api_id is None:
        missing.append(config.api_id_env_var)
    if api_hash is None:
        missing.append(config.api_hash_env_var)
    if phone_number is None:
        missing.append(config.phone_number_env_var)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing Telegram TDLib environment values: {joined}")

    assert api_id is not None
    assert api_hash is not None
    assert phone_number is not None

    state_root = resolve_tdlib_state_dir(paths, config.tdlib_state_dir) / resolved_account
    database_directory = state_root / "database"
    files_directory = state_root / "files"
    return TdlibAuthSettings(
        account=resolved_account,
        api_id=int(api_id),
        api_hash=api_hash,
        phone_number=phone_number,
        database_directory=database_directory,
        files_directory=files_directory,
        library_path=config.tdlib_library_path,
        code=read_config_env(config.code_env_var),
        password=read_config_env(config.password_env_var),
        log_verbosity_level=config.tdlib_log_verbosity_level,
    )


class TdlibJsonTransport:
    def __init__(
        self,
        *,
        library_path: str | None = None,
        log_verbosity_level: int = 0,
    ) -> None:
        resolved_library = library_path or find_library("tdjson")
        if not resolved_library:
            raise RuntimeError(
                "Could not find TDLib shared library. "
                "Set tdlib_library_path in config/sources/telegram.toml."
            )

        self._library = ctypes.CDLL(resolved_library)
        if hasattr(self._library, "td_set_log_verbosity_level"):
            self._library.td_set_log_verbosity_level.argtypes = [ctypes.c_int]
            self._library.td_set_log_verbosity_level.restype = None
            self._library.td_set_log_verbosity_level(int(log_verbosity_level))
        self._library.td_json_client_create.restype = ctypes.c_void_p
        self._library.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self._library.td_json_client_receive.restype = ctypes.c_char_p
        self._library.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_execute.restype = ctypes.c_char_p
        self._library.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        self._client = self._library.td_json_client_create()
        if not self._client:
            raise RuntimeError("Failed to create TDLib client")
        if int(log_verbosity_level) == 0:
            self.execute({"@type": "setLogStream", "log_stream": {"@type": "logStreamEmpty"}})
        self.execute(
            {
                "@type": "setLogVerbosityLevel",
                "new_verbosity_level": int(log_verbosity_level),
            }
        )

    def _encode(self, query: dict[str, Any]) -> bytes:
        return json.dumps(query, ensure_ascii=True, sort_keys=True).encode("utf-8")

    def _decode(self, payload: bytes | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        text = payload.decode("utf-8")
        if not text.strip():
            return None
        result = json.loads(text)
        if not isinstance(result, dict):
            raise RuntimeError("TDLib response must be a JSON object")
        return result

    def send(self, query: dict[str, Any]) -> None:
        self._library.td_json_client_send(self._client, self._encode(query))

    def receive(self, timeout: float) -> dict[str, Any] | None:
        return self._decode(self._library.td_json_client_receive(self._client, timeout))

    def execute(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self._decode(self._library.td_json_client_execute(self._client, self._encode(query)))

    def close(self) -> None:
        if self._client:
            self._library.td_json_client_destroy(self._client)
            self._client = None


class TdlibTelegramClient(TelegramCaptureClient):
    def __init__(
        self,
        *,
        transport: TdlibTransport,
        settings: TdlibAuthSettings,
        auth_timeout_seconds: float = AUTH_TIMEOUT_SECONDS,
        receive_timeout_seconds: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
        prompt_callback: Callable[[str, bool], str] | None = None,
        is_interactive: bool | None = None,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._auth_timeout_seconds = auth_timeout_seconds
        self._receive_timeout_seconds = receive_timeout_seconds
        self._ready = False
        self._chat_cache: dict[int, dict[str, Any]] = {}
        self._user_cache: dict[int, dict[str, Any]] = {}
        self._prompt_callback = prompt_callback or _default_prompt_callback
        self._is_interactive = os.isatty(0) if is_interactive is None else is_interactive

    def close(self) -> None:
        self._transport.close()

    def get_updates(
        self,
        *,
        after_update_id: int | None = None,
        limit: int | None = None,
    ) -> list[TelegramUpdate]:
        self._ensure_ready()
        updates: list[TelegramUpdate] = []
        next_update_id = (after_update_id or 0) + 1
        deadline = time.monotonic() + self._receive_timeout_seconds

        while True:
            timeout = max(0.0, min(self._receive_timeout_seconds, deadline - time.monotonic()))
            event = self._transport.receive(timeout)
            if event is None:
                break

            event_type = str(event.get("@type") or "")
            if event_type == "updateAuthorizationState":
                self._handle_authorization_state(event.get("authorization_state"))
                continue
            if not event_type.startswith("update"):
                continue

            payload = self._enrich_update(event)
            updates.append(
                TelegramUpdate(
                    update_type=event_type,
                    payload=payload,
                    update_id=next_update_id,
                    received_at=current_timestamp(),
                )
            )
            next_update_id += 1
            if limit is not None and len(updates) >= limit:
                break

        return updates

    def download_file(
        self, file_id: int, *, timeout_seconds: float = 120.0
    ) -> dict[str, Any] | None:
        self._ensure_ready()
        file_info = self._request(
            {"@type": "getFile", "file_id": int(file_id)}, timeout_seconds=5.0
        )
        if file_info is not None and self._file_is_ready(file_info):
            return file_info

        extra = f"download-{file_id}-{uuid4().hex}"
        self._transport.send(
            {
                "@type": "downloadFile",
                "file_id": int(file_id),
                "priority": 16,
                "offset": 0,
                "limit": 0,
                "synchronous": True,
                "@extra": extra,
            }
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            event = self._transport.receive(self._receive_timeout_seconds)
            if event is None:
                continue
            event_type = str(event.get("@type") or "")
            if event_type == "updateAuthorizationState":
                self._handle_authorization_state(event.get("authorization_state"))
                continue
            if event_type == "updateFile":
                file = event.get("file")
                if isinstance(file, dict) and int(file.get("id") or 0) == int(file_id):
                    if self._file_is_ready(file):
                        return file
                continue
            if event.get("@extra") == extra:
                if event_type == "error":
                    raise RuntimeError(f"TDLib downloadFile failed for file_id={file_id}: {event}")
                if self._file_is_ready(event):
                    return event
                local = event.get("local")
                if isinstance(local, dict) and local.get("path"):
                    return event
                continue

        raise RuntimeError(f"Timed out waiting for TDLib file download: file_id={file_id}")

    def download_remote_file(
        self,
        remote_id: str,
        *,
        kind: str,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any] | None:
        self._ensure_ready()
        queries = [
            {"@type": "getRemoteFile", "remote_file_id": remote_id},
            {
                "@type": "getRemoteFile",
                "remote_file_id": remote_id,
                "file_type": self._input_file_type(kind),
            },
        ]
        for query in queries:
            file = self._request(query, timeout_seconds=5.0)
            if file is None or str(file.get("@type") or "") == "error":
                continue
            file_id = file.get("id")
            if file_id is None:
                continue
            return self.download_file(int(file_id), timeout_seconds=timeout_seconds)
        return None

    def _request(self, query: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any] | None:
        extra = f"request-{uuid4().hex}"
        payload = dict(query)
        payload["@extra"] = extra
        self._transport.send(payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            event = self._transport.receive(self._receive_timeout_seconds)
            if event is None:
                continue
            event_type = str(event.get("@type") or "")
            if event_type == "updateAuthorizationState":
                self._handle_authorization_state(event.get("authorization_state"))
                continue
            if event.get("@extra") == extra:
                return event
        return None

    def _ensure_ready(self) -> None:
        if self._ready:
            return

        self._transport.send({"@type": "getAuthorizationState"})
        deadline = time.monotonic() + self._auth_timeout_seconds
        while time.monotonic() < deadline:
            event = self._transport.receive(self._receive_timeout_seconds)
            if event is None:
                continue
            if str(event.get("@type") or "") == "updateAuthorizationState":
                self._handle_authorization_state(event.get("authorization_state"))
                if self._ready:
                    return
                continue
            if str(event.get("@type") or "") == "error":
                raise RuntimeError(f"TDLib error during authorization: {event}")

        raise RuntimeError("Timed out waiting for TDLib authorization to become ready")

    def _handle_authorization_state(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        state_type = str(state.get("@type") or "")
        if state_type == "authorizationStateWaitTdlibParameters":
            self._settings.database_directory.mkdir(parents=True, exist_ok=True)
            self._settings.files_directory.mkdir(parents=True, exist_ok=True)
            self._transport.send(
                {
                    "@type": "setTdlibParameters",
                    "api_hash": self._settings.api_hash,
                    "api_id": self._settings.api_id,
                    "application_version": self._settings.application_version,
                    "database_directory": str(self._settings.database_directory),
                    "device_model": self._settings.device_model,
                    "files_directory": str(self._settings.files_directory),
                    "system_language_code": self._settings.system_language_code,
                    "system_version": self._settings.system_version,
                    "use_chat_info_database": True,
                    "use_file_database": True,
                    "use_message_database": True,
                    "use_secret_chats": False,
                    "use_test_dc": self._settings.use_test_dc,
                }
            )
            return
        if state_type == "authorizationStateWaitPhoneNumber":
            self._transport.send(
                {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": self._settings.phone_number,
                }
            )
            return
        if state_type == "authorizationStateWaitCode":
            if not self._settings.code and not self._is_interactive:
                raise RuntimeError(
                    "TDLib requires an authentication code. "
                    "Set TELEGRAM_AUTH_CODE or run in an interactive terminal."
                )
            code = self._settings.code or self._prompt_value(
                "Telegram sent a login code. Enter it to continue: "
            )
            self._transport.send(
                {
                    "@type": "checkAuthenticationCode",
                    "code": code,
                }
            )
            return
        if state_type == "authorizationStateWaitPassword":
            if not self._settings.password and not self._is_interactive:
                raise RuntimeError(
                    "TDLib requires a password. "
                    "Set TELEGRAM_AUTH_PASSWORD or run in an interactive terminal."
                )
            password = self._settings.password or self._prompt_value(
                "Telegram account password: ",
                hide_input=True,
            )
            self._transport.send(
                {
                    "@type": "checkAuthenticationPassword",
                    "password": password,
                }
            )
            return
        if state_type == "authorizationStateReady":
            self._ready = True
            return
        if state_type == "authorizationStateClosed":
            raise RuntimeError("TDLib authorization closed unexpectedly")

    def _enrich_update(self, event: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = dict(event)
        message = event.get("message")
        if not isinstance(message, dict):
            return payload

        payload["message"] = message
        chat_id = message.get("chat_id")
        if chat_id is not None:
            chat = self._get_chat(int(chat_id))
            if chat is not None:
                chat = dict(chat)
                participant_user_ids = self._participant_user_ids(chat, message)
                if participant_user_ids:
                    chat["participant_user_ids"] = participant_user_ids
                payload["chat"] = chat

        users = self._users_for_payload(payload, message)
        if users:
            payload["users"] = users
        return payload

    def _prompt_value(self, message: str, *, hide_input: bool = False) -> str:
        if not self._is_interactive:
            raise RuntimeError(
                "TDLib requires interactive authentication input. "
                "Run in a terminal or set the corresponding TELEGRAM_AUTH_* env var."
            )
        value = self._prompt_callback(message, hide_input).strip()
        if not value:
            raise RuntimeError("TDLib authentication input cannot be empty")
        return value

    def _file_is_ready(self, file: dict[str, Any]) -> bool:
        local = file.get("local")
        if not isinstance(local, dict):
            return False
        path = str(local.get("path") or "").strip()
        if not path:
            return False
        if local.get("is_downloading_completed") is False:
            return False
        return True

    def _input_file_type(self, kind: str) -> dict[str, Any]:
        if kind == "photo":
            return {"@type": "fileTypePhoto"}
        if kind == "document":
            return {"@type": "fileTypeDocument"}
        if kind == "voice_note":
            return {"@type": "fileTypeVoiceNote"}
        return {"@type": "fileTypeUnknown"}

    def _get_chat(self, chat_id: int) -> dict[str, Any] | None:
        if chat_id in self._chat_cache:
            return self._chat_cache[chat_id]
        chat = self._transport.execute({"@type": "getChat", "chat_id": chat_id})
        if chat is None or str(chat.get("@type") or "") == "error":
            return None
        self._chat_cache[chat_id] = chat
        return chat

    def _get_user(self, user_id: int) -> dict[str, Any] | None:
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        user = self._transport.execute({"@type": "getUser", "user_id": user_id})
        if user is None or str(user.get("@type") or "") == "error":
            return None
        self._user_cache[user_id] = user
        return user

    def _participant_user_ids(self, chat: dict[str, Any], message: dict[str, Any]) -> list[int]:
        participant_user_ids = [int(user_id) for user_id in chat.get("participant_user_ids") or []]
        if participant_user_ids:
            return sorted(set(participant_user_ids))

        chat_type = chat.get("type")
        if isinstance(chat_type, dict):
            chat_type_name = str(chat_type.get("@type") or "")
            if chat_type_name in {"chatTypePrivate", "chatTypeSecret"} and chat_type.get("user_id"):
                participant_user_ids.append(int(chat_type["user_id"]))

        sender = message.get("sender_id")
        if isinstance(sender, dict) and str(sender.get("@type") or "") == "messageSenderUser":
            user_id = sender.get("user_id")
            if user_id is not None:
                participant_user_ids.append(int(user_id))

        return sorted(set(participant_user_ids))

    def _users_for_payload(
        self, payload: dict[str, Any], message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        chat_value = payload.get("chat")
        chat: dict[str, Any] = chat_value if isinstance(chat_value, dict) else {}
        user_ids = self._participant_user_ids(chat, message)
        users: list[dict[str, Any]] = []
        for user_id in user_ids:
            user = self._get_user(user_id)
            if user is not None:
                users.append(user)
        return users


def _default_prompt_callback(message: str, hide_input: bool) -> str:
    if hide_input:
        return getpass(message)
    return input(message)
