from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class BlueBubblesSourceConfig:
    account: str = "personal"
    webhook_bind_host: str = "0.0.0.0"
    webhook_port: int = 8042
    webhook_token: str | None = None


def bluebubbles_config_path(paths: RecallPaths) -> Path:
    return paths.sources_config / "bluebubbles.toml"


def load_bluebubbles_config(paths: RecallPaths) -> BlueBubblesSourceConfig:
    config_path = bluebubbles_config_path(paths)
    if not config_path.exists():
        return BlueBubblesSourceConfig()

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    webhook_token = data.get("webhook_token")
    return BlueBubblesSourceConfig(
        account=str(data.get("account", "personal")),
        webhook_bind_host=str(data.get("webhook_bind_host", "0.0.0.0")),
        webhook_port=int(data.get("webhook_port", 8042)),
        webhook_token=str(webhook_token) if webhook_token else None,
    )
