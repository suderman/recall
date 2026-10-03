"""Explicit read-only relocation of archived references. Never rewrite evidence."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _mappings(configured: Path, modified: int, size: int) -> dict[Path, Path]:
    # Parsing once matters when validating thousands of indexed citations.
    try:
        rows = json.loads(configured.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("Expected a list of old/new paths")
        mappings = {}
        for row in rows:
            old, new = Path(row["old"]), Path(row["new"])
            if not old.is_absolute() or not new.is_absolute() or old in mappings:
                raise ValueError("Relocation paths must be absolute and old paths unique")
            mappings[old] = new
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid RECALL_RELOCATION_MAP: {configured}: {exc}") from exc
    return dict(sorted(mappings.items(), key=lambda item: len(item[0].parts), reverse=True))


def resolve_reference(path: Path) -> Path:
    path = path.expanduser().absolute()
    configured = os.getenv("RECALL_RELOCATION_MAP")
    if not configured:
        return path
    location = Path(configured).expanduser()
    try:
        stat = location.stat()
    except OSError as exc:
        raise ValueError(f"Invalid RECALL_RELOCATION_MAP: {configured}: {exc}") from exc
    return _resolve_mapped(path, location, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4096)
def _resolve_mapped(path: Path, location: Path, modified: int, size: int) -> Path:
    mappings = _mappings(location, modified, size)
    # Most specific entry wins. A recreated old path must not mask the retained archive.
    for old in mappings:
        if path.is_relative_to(old):
            return mappings[old] / path.relative_to(old)
    return path


def normalized_citation(path: Path, line: int, event: dict[str, Any]) -> tuple[Path, str | None]:
    physical = resolve_reference(path)
    try:
        with physical.open(encoding="utf-8") as stream:
            for number, text in enumerate(stream, 1):
                if number == line:
                    if json.loads(text) != event:
                        return physical, "Normalized citation does not match stored event"
                    return physical, None
        return physical, "Normalized citation line missing"
    except (OSError, UnicodeError, ValueError):
        return physical, "Normalized citation missing, unreadable or invalid"


def raw_reference(event: dict[str, Any], normalized_path: Path) -> tuple[Path | None, str | None]:
    raw = event.get("raw_ref") or {}
    if not raw.get("path") or raw["path"].startswith("local:"):
        return None, None
    path = Path(raw["path"])
    if not path.is_absolute():
        # Use the recorded root before relocation, including narrower file mappings.
        path = normalized_path.parents[3] / path
    physical = resolve_reference(path)
    if not physical.is_file():
        return physical, "Raw citation missing or not a regular file"
    return physical, None
