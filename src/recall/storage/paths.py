from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RecallPaths:
    root: Path
    config: Path
    data: Path
    raw: Path
    normalized: Path
    entities: Path
    derived: Path
    state: Path
    connectors_state: Path
    cursors: Path
    dedupe: Path
    database: Path

    @classmethod
    def from_root(cls, root: Path) -> "RecallPaths":
        root = root.expanduser().resolve()
        data = root / "data"
        state = data / "state"

        return cls(
            root=root,
            config=root / "config",
            data=data,
            raw=data / "raw",
            normalized=data / "normalized",
            entities=data / "entities",
            derived=data / "derived",
            state=state,
            connectors_state=state / "connectors",
            cursors=state / "cursors",
            dedupe=state / "dedupe",
            database=state / "recall.sqlite3",
        )

    def ensure_directories(self) -> None:
        for directory in (
            self.config,
            self.data,
            self.raw,
            self.normalized,
            self.entities,
            self.derived,
            self.state,
            self.connectors_state,
            self.cursors,
            self.dedupe,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def normalized_event_path(self, date: str) -> Path:
        return self.normalized / date[:4] / f"{date}.jsonl"
