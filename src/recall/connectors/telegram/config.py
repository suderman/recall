from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class TelegramSourceConfig:
    account: str = "personal"
    api_id_env_var: str = "TELEGRAM_API_ID"
    api_hash_env_var: str = "TELEGRAM_API_HASH"
    phone_number_env_var: str = "TELEGRAM_PHONE_NUMBER"
    code_env_var: str = "TELEGRAM_AUTH_CODE"
    password_env_var: str = "TELEGRAM_AUTH_PASSWORD"
    tdlib_state_dir: str = "data/state/telegram/tdlib"
    tdlib_library_path: str | None = None
    artifact_download_policy: str = "metadata-only"


def resolve_tdlib_state_dir(paths: RecallPaths, state_dir: str) -> Path:
    candidate = Path(state_dir).expanduser()
    if candidate.is_absolute():
        return candidate
    return paths.root / candidate


def read_config_env(env_var: str) -> str | None:
    value = os.getenv(env_var)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def telegram_config_path(paths: RecallPaths) -> Path:
    return paths.sources_config / "telegram.toml"


def load_telegram_config(paths: RecallPaths) -> TelegramSourceConfig:
    config_path = telegram_config_path(paths)
    if not config_path.exists():
        return TelegramSourceConfig()

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return TelegramSourceConfig(
        account=str(data.get("account", "personal")),
        api_id_env_var=str(data.get("api_id_env_var", "TELEGRAM_API_ID")),
        api_hash_env_var=str(data.get("api_hash_env_var", "TELEGRAM_API_HASH")),
        phone_number_env_var=str(data.get("phone_number_env_var", "TELEGRAM_PHONE_NUMBER")),
        code_env_var=str(data.get("code_env_var", "TELEGRAM_AUTH_CODE")),
        password_env_var=str(data.get("password_env_var", "TELEGRAM_AUTH_PASSWORD")),
        tdlib_state_dir=str(data.get("tdlib_state_dir", "data/state/telegram/tdlib")),
        tdlib_library_path=(
            str(data.get("tdlib_library_path")) if data.get("tdlib_library_path") else None
        ),
        artifact_download_policy=str(data.get("artifact_download_policy", "metadata-only")),
    )
