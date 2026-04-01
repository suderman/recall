from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from recall.normalize.events import RawReference


@dataclass(slots=True)
class RemoteLocator:
    kind: str
    value: str


@dataclass(slots=True)
class NormalizedArtifact:
    artifact_id: str
    source: str
    kind: str
    account: str | None = None
    source_object_id: str | None = None
    event_ids: list[str] = field(default_factory=list)
    remote_locators: list[RemoteLocator] = field(default_factory=list)
    local_path: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    checksums: dict[str, str] = field(default_factory=dict)
    download_status: str = "not_requested"
    observed_at: str | None = None
    raw_ref: RawReference | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)
