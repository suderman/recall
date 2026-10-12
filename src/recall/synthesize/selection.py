"""Versioned default event selection for bounded journal model input."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

from recall.synthesize.journal import _load_packet, model_input

RULE = "default-v2"
# Checked in order; the first matching tag names the omission reason.
DROPPED_EMAIL_TAGS = ("bulk", "marketing", "automated", "transactional", "empty_body")
# Text that looks like a credential never leaves the machine, whatever its source.
# Patterns favour recall over precision; a dropped event stays in the packet.
CREDENTIAL_LIKE = re.compile(
    r"(?i)\b(?:password|passwd|pwd|passcode|api[ _-]?key|secret[ _-]?key|access[ _-]?token"
    r"|client[ _-]?secret)\b\s*[:=]\s*\S{4,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def default_selection(
    directory: Path,
    *,
    max_bytes: int,
    measure: Callable[[str], int] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Keep non-email evidence and personal mail; drop the largest mail first to fit.

    Drops credential-like text from any source. Otherwise never drops non-email
    events, and never truncates text. Raises when even the smallest selection
    exceeds the budget. ``measure`` sizes the request actually sent; by default
    that is the encoded view.
    """
    _, events = _load_packet(directory)
    omitted: dict[str, str] = {}
    kept = []
    for row in events:
        tags = set(row.get("tags") or [])
        reason = next((tag for tag in DROPPED_EMAIL_TAGS if tag in tags), None)
        if CREDENTIAL_LIKE.search(row.get("text") or ""):
            omitted[row["event_id"]] = "credential_like"
        elif row["source"] == "email" and reason is not None:
            omitted[row["event_id"]] = f"email:{reason}"
        else:
            kept.append(row)
    removable = sorted(
        (row for row in kept if row["source"] == "email"),
        key=lambda row: (len(json.dumps(row, ensure_ascii=False)), row["event_id"]),
    )
    while True:
        selected = [row["event_id"] for row in kept if row["event_id"] not in omitted]
        content, summary = model_input(directory, event_ids=selected, max_bytes=sys.maxsize)
        size = summary["bytes"] if measure is None else measure(content)
        if size <= max_bytes:
            return selected, dict(sorted(omitted.items()))
        if not removable:
            raise ValueError(
                f"{RULE} selection uses {size} bytes without email; budget is {max_bytes}"
            )
        omitted[removable.pop()["event_id"]] = "over_budget"
