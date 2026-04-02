from __future__ import annotations

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
    tdlib_state_dir: str = "data/state/telegram/tdlib"
    artifact_download_policy: str = "metadata-only"


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
        tdlib_state_dir=str(data.get("tdlib_state_dir", "data/state/telegram/tdlib")),
        artifact_download_policy=str(data.get("artifact_download_policy", "metadata-only")),
    )
