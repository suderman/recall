from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from recall.storage.paths import RecallPaths


def bluebubbles_status(paths: RecallPaths, *, account: str) -> dict[str, Any]:
    """Read observed receipts and cursors without initializing or repairing a store."""
    errors = []
    cursors = []
    if paths.database.exists():
        try:
            with sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                cursors = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT cursor_key, cursor_value, updated_at FROM connector_cursors "
                        "WHERE source = ? AND account = ? ORDER BY cursor_key",
                        ("bluebubbles", account),
                    )
                ]
        except sqlite3.Error:
            errors.append({"kind": "cursor_state_unreadable", "path": str(paths.database)})
    receipts = 0
    messages = 0
    guids = set()
    dates = set()
    last_received = None
    last_webhook = None
    modes = Counter()
    files = sorted((paths.raw / "bluebubbles").glob("????-??-??/events.jsonl"))
    for path in files:
        try:
            with path.open(encoding="utf-8") as stream:
                for line, text in enumerate(stream, 1):
                    if not text.strip():
                        continue
                    try:
                        row = json.loads(text)
                        if not isinstance(row, dict):
                            raise ValueError("Not an envelope")
                        if row.get("source") != "bluebubbles" or row.get("account") != account:
                            continue
                        stamp = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            raise ValueError("Receipt timestamp has no timezone")
                        stamp = stamp.astimezone(timezone.utc)
                        if not isinstance(row.get("event_type"), str) or not isinstance(
                            row.get("payload"), dict
                        ):
                            raise ValueError("Invalid envelope")
                        data = row["payload"].get("data", row["payload"])
                        is_message = row["event_type"] in {
                            "new-message",
                            "historical-message",
                            "recovered-message",
                        }
                        if is_message and (
                            not isinstance(data, dict)
                            or not isinstance(data.get("guid"), str)
                            or not data["guid"]
                        ):
                            raise ValueError("Invalid message envelope")
                    except (ValueError, KeyError, TypeError, AttributeError):
                        errors.append(
                            {"kind": "raw_record_invalid", "path": str(path), "line": line}
                        )
                        continue
                    receipts += 1
                    dates.add(path.parent.name)
                    mode = row.get("capture_mode")
                    modes[mode if mode in ("webhook", "recovery", "import") else "other"] += 1
                    last_received = max(last_received, stamp) if last_received else stamp
                    if mode == "webhook":
                        last_webhook = max(last_webhook, stamp) if last_webhook else stamp
                    if is_message:
                        messages += 1
                        guids.add(data["guid"])
        except (OSError, UnicodeError):
            errors.append({"kind": "raw_file_unreadable", "path": str(path)})
    return {
        "source": "bluebubbles",
        "account": account,
        "cursors": cursors,
        "raw_files": len(files),
        "receipts": receipts,
        "message_receipts": messages,
        "unique_message_guids": len(guids),
        "raw_dates": sorted(dates),
        "capture_modes": dict(sorted(modes.items())),
        "last_webhook_received_at": (
            last_webhook.isoformat().replace("+00:00", "Z") if last_webhook else None
        ),
        "last_received_at": (
            last_received.isoformat().replace("+00:00", "Z") if last_received else None
        ),
        "errors": errors,
        "coverage_note": (
            "Observed receipts and cursors are not proof of complete or current delivery."
        ),
    }
