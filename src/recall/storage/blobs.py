from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def replace_blob(path: Path, chunks: Iterable[bytes]) -> tuple[str, int]:
    """Stage complete bytes beside the destination before replacing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    checksum = hashlib.sha256()
    size = 0
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            for chunk in chunks:
                handle.write(chunk)
                checksum.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return checksum.hexdigest(), size


def cache_error(path: Path, artifact: dict[str, Any]) -> str | None:
    """Check acquired bytes without changing evidence or attempting repair."""
    if not path.is_file():
        return "Artifact cache bytes missing or not a regular file"
    expected = (artifact.get("checksums") or {}).get("sha256")
    if not expected:
        return "Artifact cache unverifiable: missing SHA-256 checksum"
    try:
        size = artifact.get("size_bytes")
        if size is not None and path.stat().st_size != size:
            return "Artifact cache size mismatch"
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected:
            return "Artifact cache SHA-256 mismatch"
    except OSError:
        return "Artifact cache bytes unreadable"
    return None
