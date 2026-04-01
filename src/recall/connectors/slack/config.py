from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class SlackSourceConfig:
    account: str = "default"
    token_env_var: str = "SLACK_USER_TOKEN"
    include_archived: bool = False
    artifact_download_policy: str = "metadata-only"


def slack_config_path(paths: RecallPaths) -> Path:
    return paths.sources_config / "slack.toml"


def load_slack_config(paths: RecallPaths) -> SlackSourceConfig:
    config_path = slack_config_path(paths)
    if not config_path.exists():
        return SlackSourceConfig()

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return SlackSourceConfig(
        account=str(data.get("account", "default")),
        token_env_var=str(data.get("token_env_var", "SLACK_USER_TOKEN")),
        include_archived=bool(data.get("include_archived", False)),
        artifact_download_policy=str(data.get("artifact_download_policy", "metadata-only")),
    )
