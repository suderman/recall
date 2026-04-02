from __future__ import annotations

import hashlib
from pathlib import Path

from recall.connectors.email.notmuch import NotmuchRunner, load_email_messages, parse_date
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_normalized_events
from recall.storage.paths import RecallPaths


def _identity_id(address: str) -> str:
    normalized = []
    for char in address.strip().lower():
        normalized.append(char if char.isalnum() else "_")
    compact = "".join(normalized).strip("_")
    while "__" in compact:
        compact = compact.replace("__", "_")
    return f"ident_email_{compact}"


def _stable_event_id(account: str, message_id: str) -> str:
    payload = f"email:{account}:{message_id}".encode("utf-8")
    return f"evt_{hashlib.sha256(payload).hexdigest()[:20]}"


def normalize_email_day(
    paths: RecallPaths,
    *,
    date: str,
    account: str = "default",
    extra_query: str | None = None,
    runner: NotmuchRunner | None = None,
) -> Path:
    parse_date(date)
    paths.ensure_directories()
    messages = load_email_messages(date=date, extra_query=extra_query, runner=runner)
    events: list[NormalizedEvent] = []

    for message in messages:
        sender_identity_id = (
            _identity_id(message.sender.address) if message.sender is not None else None
        )
        participant_identity_ids = [
            _identity_id(address.address) for address in message.participant_addresses
        ]
        raw_ref = RawReference(
            source="email",
            path=str(message.file_path),
            locator={"message_id": message.message_id},
        )
        raw_fragment = {
            "subject": message.subject,
            "sender": message.sender.address if message.sender else None,
            "to": [address.address for address in message.to_addresses],
            "cc": [address.address for address in message.cc_addresses],
            "headers": message.headers,
        }
        tags = list(message.tags)
        events.append(
            NormalizedEvent(
                event_id=_stable_event_id(account, message.message_id),
                source="email",
                account=account,
                timestamp=message.timestamp,
                date=message.date,
                kind="email",
                conversation_id=message.conversation_id,
                conversation_label=message.subject or None,
                thread_id=message.conversation_id,
                sender_identity_id=sender_identity_id,
                participant_identity_ids=participant_identity_ids,
                text=message.text or None,
                source_urls=message.source_urls,
                artifact_ids=[],
                raw_ref=raw_ref,
                raw_fragment=raw_fragment,
                tags=tags,
            )
        )

    return write_normalized_events(paths, date, events, merge_existing=True)
