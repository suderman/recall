from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class TelegramArtifactClient(Protocol):
    def download_file(
        self, file_id: int, *, timeout_seconds: float = 120.0
    ) -> dict[str, Any] | None: ...

    def download_remote_file(
        self,
        remote_id: str,
        *,
        kind: str,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TelegramArtifactDownloadResult:
    artifacts_seen: int
    would_download: int
    downloaded: int
    skipped_policy: int
    skipped_existing: int
    failed: int
    artifact_path: Path


def _sanitize_filename(value: str | None, artifact_id: str) -> str:
    base = value or artifact_id
    sanitized = SAFE_FILENAME_PATTERN.sub("_", base).strip("._")
    return sanitized or artifact_id


def _blob_path(paths: RecallPaths, *, date: str, artifact: dict[str, Any]) -> Path:
    filename = _sanitize_filename(artifact.get("filename"), str(artifact["artifact_id"]))
    return (
        paths.artifacts_blobs
        / "telegram"
        / date[:4]
        / date
        / f"{artifact['artifact_id']}--{filename}"
    )


def _choose_local_source_path(artifact: dict[str, Any]) -> Path | None:
    for locator in artifact.get("remote_locators", []):
        if locator.get("kind") != "local_path":
            continue
        value = str(locator.get("value") or "").strip()
        if not value:
            continue
        candidate = Path(value)
        if candidate.exists():
            return candidate
    return None


def _remote_id(artifact: dict[str, Any]) -> str | None:
    for locator in artifact.get("remote_locators", []):
        if locator.get("kind") != "remote_id":
            continue
        value = str(locator.get("value") or "").strip()
        if value:
            return value
    return None


def _copy_with_checksum(source_path: Path, destination_path: Path) -> str:
    checksum = hashlib.sha256()
    with source_path.open("rb") as source_handle, destination_path.open("wb") as destination_handle:
        while True:
            chunk = source_handle.read(1024 * 1024)
            if not chunk:
                break
            destination_handle.write(chunk)
            checksum.update(chunk)
    return checksum.hexdigest()


def download_telegram_artifacts(
    paths: RecallPaths,
    *,
    date: str,
    policy: str,
    client: TelegramArtifactClient | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> TelegramArtifactDownloadResult:
    artifact_path = paths.artifact_metadata_path("telegram", date)
    if not artifact_path.exists():
        raise FileNotFoundError(f"No artifact metadata file found at {artifact_path}")

    artifacts = read_jsonl(artifact_path)
    would_download = 0
    downloaded = 0
    skipped_policy = 0
    skipped_existing = 0
    failed = 0

    for artifact in artifacts:
        artifact["download_status"] = artifact.get("download_status") or "not_requested"
        if policy != "download-source-native":
            artifact["download_status"] = "not_requested"
            artifact["last_error"] = None
            skipped_policy += 1
            continue

        blob_path = _blob_path(paths, date=date, artifact=artifact)
        if blob_path.exists() and artifact.get("download_status") == "downloaded" and not force:
            artifact["local_path"] = paths.relative_to_root(blob_path)
            artifact["last_error"] = None
            skipped_existing += 1
            continue

        would_download += 1
        if dry_run:
            artifact["last_error"] = None
            continue

        source_path = _choose_local_source_path(artifact)
        tdlib_errors: list[str] = []
        if source_path is None:
            source_object_id = str(artifact.get("source_object_id") or "").strip()
            if client is not None and source_object_id.isdigit():
                try:
                    file = client.download_file(int(source_object_id))
                except Exception as exc:
                    tdlib_errors.append(f"file_id {source_object_id}: {exc}")
                    file = None
                if isinstance(file, dict):
                    local_value = file.get("local")
                    local: dict[str, Any] = local_value if isinstance(local_value, dict) else {}
                    local_value = str(local.get("path") or "").strip()
                    if local_value:
                        candidate = Path(local_value)
                        if candidate.exists():
                            source_path = candidate
            if source_path is None and client is not None:
                remote_id = _remote_id(artifact)
                if remote_id:
                    try:
                        file = client.download_remote_file(
                            remote_id, kind=str(artifact.get("kind") or "file")
                        )
                    except Exception as exc:
                        tdlib_errors.append(f"remote_id {remote_id}: {exc}")
                        file = None
                    if isinstance(file, dict):
                        local_value = file.get("local")
                        local: dict[str, Any] = local_value if isinstance(local_value, dict) else {}
                        local_value = str(local.get("path") or "").strip()
                        if local_value:
                            candidate = Path(local_value)
                            if candidate.exists():
                                source_path = candidate

        if source_path is None:
            artifact["download_status"] = "not_available"
            artifact["last_error"] = (
                "; ".join(tdlib_errors)
                if tdlib_errors
                else ("No Telegram local file, file id, or remote id produced a downloadable file")
            )
            failed += 1
            continue

        blob_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            checksum = _copy_with_checksum(source_path, blob_path)
        except Exception as exc:
            failed += 1
            artifact["download_status"] = "failed"
            artifact["last_error"] = str(exc)
            if blob_path.exists():
                blob_path.unlink()
            continue

        artifact["local_path"] = paths.relative_to_root(blob_path)
        artifact["checksums"] = {"sha256": checksum}
        artifact["download_status"] = "downloaded"
        artifact["last_error"] = None
        downloaded += 1

    if not dry_run:
        write_jsonl(artifact_path, artifacts)
    return TelegramArtifactDownloadResult(
        artifacts_seen=len(artifacts),
        would_download=would_download,
        downloaded=downloaded,
        skipped_policy=skipped_policy,
        skipped_existing=skipped_existing,
        failed=failed,
        artifact_path=artifact_path,
    )
