from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RecallPaths:
    root: Path
    config: Path
    sources_config: Path
    data: Path
    raw: Path
    normalized: Path
    artifacts: Path
    artifacts_metadata: Path
    artifacts_blobs: Path
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
            sources_config=root / "config" / "sources",
            data=data,
            raw=data / "raw",
            normalized=data / "normalized",
            artifacts=data / "artifacts",
            artifacts_metadata=data / "artifacts" / "metadata",
            artifacts_blobs=data / "artifacts" / "blobs",
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
            self.sources_config,
            self.data,
            self.raw,
            self.normalized,
            self.artifacts,
            self.artifacts_metadata,
            self.artifacts_blobs,
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

    def artifact_metadata_path(self, source: str, date: str) -> Path:
        return self.artifacts_metadata / source / date[:4] / f"{date}.jsonl"

    def raw_capture_dir(self, source: str, date: str) -> Path:
        return self.raw / source / date

    def raw_import_dir(self, source: str, import_id: str) -> Path:
        return self.raw / source / "imports" / import_id

    def relative_to_root(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()
