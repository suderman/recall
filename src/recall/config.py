from __future__ import annotations

import os
from pathlib import Path


RECALL_ROOT_ENV = "RECALL_ROOT"


def resolve_root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser().resolve()

    configured_root = os.getenv(RECALL_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    return Path.cwd().resolve()
