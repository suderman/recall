from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

SLACK_API_BASE_URL = "https://slack.com/api/"


class SlackApiClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._sleep = sleep_fn
        self._client = client or httpx.Client(base_url=SLACK_API_BASE_URL, timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SlackApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = {
            key: value for key, value in (params or {}).items() if value is not None and value != ""
        }

        while True:
            response = self._client.get(
                method,
                params=request_params,
                headers={"Authorization": f"Bearer {self._token}"},
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after", "30"))
                self._sleep(retry_after)
                continue

            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"{method} failed: {payload.get('error', 'unknown_error')}")
            return payload

    def auth_test(self) -> dict[str, Any]:
        return self._get("auth.test")

    def list_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = self._get("users.list", {"limit": 200, "cursor": cursor})
            users.extend(page.get("members", []))
            cursor = page.get("response_metadata", {}).get("next_cursor") or None
            if cursor is None:
                return users

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        conversations: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = self._get(
                "users.conversations",
                {
                    "types": "public_channel,private_channel,im,mpim",
                    "exclude_archived": str(not include_archived).lower(),
                    "limit": 200,
                    "cursor": cursor,
                },
            )
            conversations.extend(page.get("channels", []))
            cursor = page.get("response_metadata", {}).get("next_cursor") or None
            if cursor is None:
                return conversations

    def fetch_history(
        self,
        channel_id: str,
        *,
        oldest: str,
        latest: str,
        inclusive: bool = True,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = self._get(
                "conversations.history",
                {
                    "channel": channel_id,
                    "oldest": oldest,
                    "latest": latest,
                    "inclusive": str(inclusive).lower(),
                    "limit": 200,
                    "cursor": cursor,
                },
            )
            messages.extend(page.get("messages", []))
            cursor = page.get("response_metadata", {}).get("next_cursor") or None
            if cursor is None:
                return messages

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, Any]]:
        replies: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = self._get(
                "conversations.replies",
                {
                    "channel": channel_id,
                    "ts": ts,
                    "limit": 200,
                    "cursor": cursor,
                },
            )
            replies.extend(page.get("messages", []))
            cursor = page.get("response_metadata", {}).get("next_cursor") or None
            if cursor is None:
                return replies
