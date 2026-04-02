from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recall.connectors.bluebubbles.capture import (
    append_bluebubbles_envelope,
    local_date_for_timestamp,
)
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class BlueBubblesImportResult:
    import_id: str
    import_dir: Path
    dates_written: list[str]
    messages_imported: int


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _export_id(export_path: Path, manifest: dict[str, Any]) -> str:
    configured = manifest.get("export_id")
    if configured:
        return str(configured)
    payload = str(export_path.resolve()).encode("utf-8")
    return f"bluebubbles_export_{hashlib.sha256(payload).hexdigest()[:12]}"


def _message_received_at(message: dict[str, Any]) -> str:
    value = message.get("dateCreated") or message.get("date")
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    numeric = float(value)
    if numeric > 1_000_000_000_000:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def import_bluebubbles_export(
    paths: RecallPaths,
    *,
    export_path: Path,
    account: str,
) -> BlueBubblesImportResult:
    paths.ensure_directories()
    export_dir = export_path.expanduser().resolve()
    if not export_dir.is_dir():
        raise FileNotFoundError(f"BlueBubbles export directory not found: {export_dir}")

    manifest_path = export_dir / "manifest.json"
    messages_path = export_dir / "messages.jsonl"
    if not manifest_path.exists() or not messages_path.exists():
        raise FileNotFoundError(
            f"BlueBubbles export requires manifest.json and messages.jsonl in {export_dir}"
        )

    manifest = _load_json(manifest_path)
    messages = _load_jsonl(messages_path)
    import_id = _export_id(export_dir, manifest)
    import_dir = paths.raw_import_dir("bluebubbles", import_id)
    if import_dir.exists():
        raise FileExistsError(
            "BlueBubbles export import already exists at "
            f"{import_dir}; choose a new export_id or remove it first"
        )

    shutil.copytree(export_dir, import_dir)

    dates_written: set[str] = set()
    for message in messages:
        received_at = _message_received_at(message)
        date = local_date_for_timestamp(received_at)
        envelope = {
            "received_at": received_at,
            "source": "bluebubbles",
            "account": account,
            "capture_mode": "import",
            "import_id": import_id,
            "event_type": "historical-message",
            "payload": {
                "type": "historical-message",
                "data": message,
            },
        }
        append_bluebubbles_envelope(paths, date=date, envelope=envelope)
        dates_written.add(date)

    return BlueBubblesImportResult(
        import_id=import_id,
        import_dir=import_dir,
        dates_written=sorted(dates_written),
        messages_imported=len(messages),
    )
