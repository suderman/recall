from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall.connectors.asana.capture import append_asana_envelope, local_date_for_timestamp
from recall.storage.paths import RecallPaths


@dataclass(frozen=True, slots=True)
class AsanaImportResult:
    import_id: str
    import_dir: Path
    dates_written: list[str]
    tasks_imported: int
    stories_imported: int


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Asana export in {path} must be a JSON object")
    return payload


def _prepare_export_dir(export_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    resolved = export_path.expanduser().resolve()
    if resolved.is_dir():
        return resolved, None
    if resolved.is_file() and resolved.suffix.lower() == ".zip":
        tempdir = tempfile.TemporaryDirectory(prefix="recall-asana-export-")
        with zipfile.ZipFile(resolved) as archive:
            archive.extractall(tempdir.name)
        return Path(tempdir.name), tempdir
    raise FileNotFoundError(f"Asana export must be a directory or .zip file: {resolved}")


def _find_export_json(export_root: Path) -> Path:
    direct = export_root / "export.json"
    if direct.exists():
        return direct
    matches = sorted(export_root.glob("**/export.json"))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"Asana export requires export.json in {export_root}")


def _import_id(source_path: Path, payload: dict[str, Any]) -> str:
    export_name = str(payload.get("name") or source_path.stem or source_path.name).strip().lower()
    normalized = "_".join(part for part in export_name.split() if part) or "asana"
    digest = hashlib.sha256(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"asana_export_{normalized}_{digest}"


def _copy_export(source_root: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(
            "Asana export import already exists at "
            f"{destination}; remove it first or use a new export"
        )
    shutil.copytree(source_root, destination)


def _task_timestamp(task: dict[str, Any]) -> str:
    return str(task.get("created_at") or task.get("modified_at") or task.get("completed_at") or "")


def _actor_from_story(story: dict[str, Any]) -> dict[str, Any] | None:
    actor = story.get("created_by")
    return actor if isinstance(actor, dict) else None


def import_asana_export(
    paths: RecallPaths,
    *,
    export_path: Path,
    account: str,
) -> AsanaImportResult:
    paths.ensure_directories()
    export_root, tempdir = _prepare_export_dir(export_path)
    try:
        export_json = _find_export_json(export_root)
        payload = _load_json(export_json)
        import_id = _import_id(export_path, payload)
        import_dir = paths.raw_import_dir("asana", import_id)
        _copy_export(export_json.parent, import_dir)

        dates_written: set[str] = set()
        tasks_imported = 0
        stories_imported = 0

        for task in payload.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            task_timestamp = _task_timestamp(task)
            if not task_timestamp:
                continue
            task_date = local_date_for_timestamp(task_timestamp)
            append_asana_envelope(
                paths,
                date=task_date,
                envelope={
                    "received_at": task_timestamp,
                    "source": "asana",
                    "account": account,
                    "capture_mode": "import",
                    "import_id": import_id,
                    "event_type": "task",
                    "payload": task,
                },
            )
            dates_written.add(task_date)
            tasks_imported += 1

            if task.get("completed") and task.get("completed_at"):
                completed_at = str(task["completed_at"])
                completed_date = local_date_for_timestamp(completed_at)
                append_asana_envelope(
                    paths,
                    date=completed_date,
                    envelope={
                        "received_at": completed_at,
                        "source": "asana",
                        "account": account,
                        "capture_mode": "import",
                        "import_id": import_id,
                        "event_type": "task_completion",
                        "payload": task,
                    },
                )
                dates_written.add(completed_date)

            for story in task.get("stories") or []:
                if not isinstance(story, dict):
                    continue
                story_timestamp = str(story.get("created_at") or "")
                if not story_timestamp:
                    continue
                story_date = local_date_for_timestamp(story_timestamp)
                append_asana_envelope(
                    paths,
                    date=story_date,
                    envelope={
                        "received_at": story_timestamp,
                        "source": "asana",
                        "account": account,
                        "capture_mode": "import",
                        "import_id": import_id,
                        "event_type": "task_story",
                        "payload": {
                            "task_gid": task.get("gid"),
                            "task_name": task.get("name"),
                            "story": story,
                            "task_participants": {
                                "assignee": task.get("assignee"),
                                "created_by": task.get("created_by"),
                                "followers": task.get("followers") or [],
                            },
                            "actor": _actor_from_story(story),
                        },
                    },
                )
                dates_written.add(story_date)
                stories_imported += 1

        return AsanaImportResult(
            import_id=import_id,
            import_dir=import_dir,
            dates_written=sorted(dates_written),
            tasks_imported=tasks_imported,
            stories_imported=stories_imported,
        )
    finally:
        if tempdir is not None:
            tempdir.cleanup()
