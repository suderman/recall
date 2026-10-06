"""Read-only writing candidates. Nothing here admits text to a voice corpus."""

from __future__ import annotations

import hashlib
import json
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from recall.connectors.email.normalize import _identity_id
from recall.connectors.telegram.voice import candidate as telegram_candidate
from recall.normalize.rebuild import date_range
from recall.storage.paths import RecallPaths
from recall.storage.references import raw_reference, resolve_reference

# Only review the leading passage. Inline answers after a quote remain excluded.
TAILS = {
    "quoted_tail": re.compile(
        r"(?im)^[ \t]*>|^On [^\n]{0,300}(?:\n[^\n]{0,300}){0,4}?wrote:[ \t]*\r?$"
    ),
    "forwarded_tail": re.compile(
        r"(?im)^[-_]{2,}[^\n]*(?:forwarded message|original message)[^\n]*$"
        r"|^From:[^\n]*\n(?:Sent|Date):[^\n]*$|^_{5,}[ \t]*\r?$"
    ),
    "signature": re.compile(
        r"(?im)^[ \t]*-- \r?$|^[ \t]*(?:best(?: regards)?|kind regards|regards|cheers|sincerely"
        r"|thanks(?: again)?|thank you|take care)[,!]?[ \t]*\r?\n"
        r"(?:[ \t]*\r?\n)*[ \t]*\S[^\n]*"
    ),
}


def _passage(text: str, headers: dict[str, str], sender_name: str) -> dict[str, Any]:
    boundaries = [
        (match.start(), reason)
        for reason, pattern in TAILS.items()
        if (match := pattern.search(text))
    ]
    cut, reason = min(boundaries) if boundaries else (len(text), None)
    head = text[:cut]
    # A separate final paragraph matching the raw sender's name is a possible sign-off,
    # not confirmed prose. Do not remove names embedded in sentences or lists.
    name = " ".join(sender_name.split())
    for alias in [name, *name.split()[:1]]:
        if not alias:
            continue
        match = re.search(r"(?im)^[ \t]*" + re.escape(alias) + r"[ \t]*(?:\r?\n)*\Z", head)
        if match and (match.start() == 0 or re.search(r"\r?\n[ \t]*\r?\n$", head[: match.start()])):
            cut, reason = match.start(), "sender_name_tail"
            head = text[:cut]
            break
    start, end = len(head) - len(head.lstrip()), len(head.rstrip())
    exclusions = [{"start": cut, "end": len(text), "reason": reason}] if reason else []
    result: dict[str, Any] = {"passage": None, "exclusions": exclusions}
    if not head.strip():
        return {**result, "reason": "no_original_passage"}
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|```|~~~", head):
        return {**result, "reason": "non_prose"}
    if re.search(
        r"(?im)^[^\r\n]*<[^<>\r\n]+@[^<>\r\n]+>:[ \t]*\r?$|^\[image\][ \t]*\r?$",
        head,
    ):
        return {**result, "reason": "ambiguous_attribution"}
    if re.search(r'["“”«»]', head):
        return {**result, "reason": "ambiguous_quotation"}
    if headers.get("subject", "").lower().startswith(("fw:", "fwd:")) and not any(
        name == "forwarded_tail" for _, name in boundaries
    ):
        return {**result, "reason": "forward_boundary_unknown"}
    if (headers.get("in-reply-to") or headers.get("references")) and not any(
        name in {"quoted_tail", "forwarded_tail"} for _, name in boundaries
    ):
        return {**result, "reason": "reply_boundary_unknown"}
    value = text[start:end]
    return {
        **result,
        "passage": {
            "text": value,
            "start": start,
            "end": end,
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
        },
    }


def _email(event: dict[str, Any], normalized: Path) -> dict[str, Any]:
    empty = {"passage": None, "exclusions": []}
    raw = event.get("raw_ref")
    if (
        not isinstance(raw, dict)
        or raw.get("source") != "email"
        or not isinstance(raw.get("path"), str)
    ):
        return {**empty, "reason": "raw_unverifiable"}
    locator = raw.get("locator")
    if (
        not isinstance(locator, dict)
        or not isinstance(locator.get("message_id"), str)
        or not locator["message_id"].strip().strip("<>")
    ):
        return {**empty, "reason": "raw_unverifiable"}
    try:
        path, error = raw_reference(event, normalized)
        if path is None or error:
            return {**empty, "reason": "raw_unverifiable"}
        data = path.read_bytes()
        message = BytesParser(policy=policy.default).parsebytes(data)
        ids = message.get_all("Message-ID", [])
        if len(ids) != 1 or str(ids[0]).strip().strip("<>") != locator["message_id"].strip("<>"):
            return {**empty, "reason": "raw_message_id_mismatch"}
        senders = message.get_all("From", [])
        addresses = getaddresses([str(value) for value in senders])
        if (
            len(senders) != 1
            or len(addresses) != 1
            or not addresses[0][1]
            or (_identity_id(addresses[0][1]) != event["sender_identity_id"])
        ):
            return {**empty, "reason": "raw_sender_mismatch"}
        if any(
            len(message.get_all(name, [])) > 1
            for name in ("Subject", "Auto-Submitted", "Precedence", "In-Reply-To", "References")
        ):
            return {**empty, "reason": "ambiguous_headers"}
        headers = {key.lower(): str(value) for key, value in message.items()}
        if (
            headers.get("auto-submitted", "").lower() not in {"", "no"}
            or (headers.get("precedence", "").lower() in {"bulk", "list", "junk"})
            or headers.get("list-id")
            or headers.get("list-unsubscribe")
        ):
            return {**empty, "reason": "automated"}
        parts = [
            part
            for part in message.walk()
            if part.get_content_type() == "text/plain"
            and part.get_content_disposition() != "attachment"
        ]
        if len(parts) != 1 or message.defects or parts[0].defects:
            return {**empty, "reason": "plain_body_unverifiable"}
        text = parts[0].get_content()
        if not isinstance(text, str):
            return {**empty, "reason": "plain_body_unverifiable"}
        # Normalization strips body whitespace, but passage offsets refer to the decoded MIME body.
        if text.strip() != (event.get("text") or ""):
            return {**empty, "reason": "raw_text_mismatch"}
        if path.read_bytes() != data:
            return {**empty, "reason": "raw_changed_during_read"}
        return {
            **_passage(text, headers, addresses[0][0]),
            "raw_path": str(path),
            "raw_sha256": hashlib.sha256(data).hexdigest(),
            "raw_sender": addresses[0][1],
            "mime_part": 0 if not message.is_multipart() else list(message.walk()).index(parts[0]),
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    except (OSError, UnicodeError, LookupError, ValueError):
        return {**empty, "reason": "raw_unverifiable"}


def candidates(
    paths: RecallPaths,
    *,
    identities: list[str],
    first: str,
    last: str,
    account: str | None = None,
    source: str | None = None,
    limit: int = 20,
    exclude_events: list[str] | None = None,
) -> dict[str, Any]:
    """Inspect explicitly selected senders/days without config, SQLite or model access."""
    days = date_range(first, last)
    if not identities or any(not value.strip() or value != value.strip() for value in identities):
        raise ValueError("Choose explicit nonblank sender identity IDs")
    if source not in {None, "email", "telegram"}:
        raise ValueError("Choose email or telegram as the voice candidate source")
    if source == "telegram" and (not account or not account.strip() or account != account.strip()):
        raise ValueError("Telegram candidates require an explicit nonblank account")
    if limit < 1:
        raise ValueError("Limit must be positive")
    records, missing = [], []
    truncated = False
    for day in days:
        recorded = paths.normalized_event_path(day)
        physical = resolve_reference(recorded)
        if not physical.exists():
            missing.append(day)
            continue
        try:
            content = physical.read_bytes()
            lines = content.decode().splitlines()
            for line, text in enumerate(lines, 1):
                if not text.strip():
                    continue
                event = json.loads(text)
                if (
                    not isinstance(event, dict)
                    or any(
                        not isinstance(event.get(key), str)
                        for key in ("event_id", "source", "kind", "date", "timestamp")
                    )
                    or event["date"] != day
                ):
                    raise ValueError("Invalid event shape or day")
                sender = event.get("sender_identity_id")
                if (
                    sender not in identities
                    or (account is not None and event.get("account") != account)
                    or (source is not None and event["source"] != source)
                ):
                    continue
                if len(records) >= limit:
                    truncated = True
                    continue
                item: dict[str, Any] = {
                    "event_id": event["event_id"],
                    "identity_id": sender,
                    "source": event["source"],
                    "account": event.get("account"),
                    "date": day,
                    "timestamp": event["timestamp"],
                    "normalized_path": str(recorded),
                    "resolved_normalized_path": str(physical),
                    "line": line,
                    "normalized_sha256": hashlib.sha256(content).hexdigest(),
                    "raw_ref": event.get("raw_ref"),
                    "corpus_eligible": False,
                    "passage": None,
                    "exclusions": [],
                }
                tags = event.get("tags") or []
                if event["event_id"] in (exclude_events or []):
                    item["reason"] = "explicitly_excluded"
                elif any(tag in tags for tag in ("ai_generated", "ai_assisted", "generated")):
                    item["reason"] = "known_generated"
                elif any(
                    tag in tags for tag in ("automated", "bulk", "marketing", "transactional")
                ):
                    item["reason"] = "automated"
                elif source == "telegram" and event["kind"] == "message":
                    item.update(telegram_candidate(event, recorded))
                elif event["source"] != "email" or event["kind"] != "email":
                    item["reason"] = "unsupported_source"
                else:
                    item.update(_email(event, recorded))
                item["status"] = "needs_review" if item["passage"] is not None else "excluded"
                records.append(item)
            if physical.read_bytes() != content:
                raise ValueError("Normalized input changed during inspection")
        except (OSError, UnicodeError, ValueError, TypeError):
            # Never echo malformed records, raw exception text or private message bodies.
            raise ValueError(
                f"Normalized input missing, unreadable or invalid: {physical}"
            ) from None
    return {
        "format": "recall-voice-candidates-v1",
        "extractor_version": 1 if source == "telegram" else 2,
        "scope": {
            **({"source": source} if source is not None else {}),
            "identities": sorted(set(identities)),
            "first": first,
            "last": last,
            "account": account,
            "limit": limit,
            "exclude_events": exclude_events or [],
        },
        "counts": {
            state: sum(item["status"] == state for item in records)
            for state in ("needs_review", "excluded")
        },
        "missing_days": missing,
        "truncated": truncated,
        "records": records,
        "note": (
            "Native Telegram plain-text candidates only. "
            if source == "telegram"
            else "Email-first candidates only. "
        )
        + "Human authorship confirmation is required; "
        "no record is corpus-eligible. AI assistance cannot be detected automatically. "
        "Missing days and a truncated report do not establish source coverage.",
        "offsets": (
            "Zero-based Unicode offsets in the raw messageText body; end-exclusive."
            if source == "telegram"
            else "Zero-based Unicode offsets in the decoded plain MIME part; end-exclusive."
        ),
    }
