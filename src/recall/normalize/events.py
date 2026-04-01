from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class NormalizedEvent:
    event_id: str
    source: str
    timestamp: str
    date: str
    kind: str
    account: str | None = None
    conversation_id: str | None = None
    conversation_label: str | None = None
    thread_id: str | None = None
    sender_person_id: str | None = None
    sender_identity_id: str | None = None
    participant_person_ids: list[str] = field(default_factory=list)
    participant_identity_ids: list[str] = field(default_factory=list)
    title: str | None = None
    text: str | None = None
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    location: str | None = None
    raw_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)
