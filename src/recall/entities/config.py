from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class ManualIdentityResolutionRule:
    source: str
    kind: str
    value: str
    person_id: str
    confidence: str = "high"
    method: str = "manual_override"
    evidence: list[str] = field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass(frozen=True, slots=True)
class ManualPersonMergeRule:
    from_person_id: str
    to_person_id: str
    reason: str = "manual merge"


@dataclass(frozen=True, slots=True)
class EntityResolutionConfig:
    identity_resolutions: list[ManualIdentityResolutionRule] = field(default_factory=list)
    person_merges: list[ManualPersonMergeRule] = field(default_factory=list)


def load_entity_resolution_config(paths: RecallPaths) -> EntityResolutionConfig:
    config_path = paths.entity_resolution_config / "manual.toml"
    if not config_path.exists():
        return EntityResolutionConfig()

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    identity_resolutions = [
        ManualIdentityResolutionRule(
            source=str(row["source"]),
            kind=str(row["kind"]),
            value=str(row["value"]),
            person_id=str(row["person_id"]),
            confidence=str(row.get("confidence", "high")),
            method=str(row.get("method", "manual_override")),
            evidence=[str(item) for item in row.get("evidence", [])],
            valid_from=str(row["valid_from"]) if row.get("valid_from") else None,
            valid_to=str(row["valid_to"]) if row.get("valid_to") else None,
        )
        for row in data.get("identity_resolution", [])
    ]
    person_merges = [
        ManualPersonMergeRule(
            from_person_id=str(row["from_person_id"]),
            to_person_id=str(row["to_person_id"]),
            reason=str(row.get("reason", "manual merge")),
        )
        for row in data.get("person_merge", [])
    ]
    return EntityResolutionConfig(
        identity_resolutions=identity_resolutions,
        person_merges=person_merges,
    )
