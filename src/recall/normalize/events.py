from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class RawReference:
    source: str
    path: str
    locator: dict[str, Any]


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
    sender_identity_id: str | None = None
    participant_identity_ids: list[str] = field(default_factory=list)
    text: str | None = None
    source_urls: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    raw_ref: RawReference | None = None
    raw_fragment: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)
