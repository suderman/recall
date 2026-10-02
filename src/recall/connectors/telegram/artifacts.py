from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from recall.connectors.telegram.normalize import _file_details, _message
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


def _native_artifact_ids(paths: RecallPaths, artifacts: list[dict[str, Any]]) -> set[str]:
    # Read each referenced capture once, retaining physical line numbers.
    references: dict[Path, dict[int, list[dict[str, Any]]]] = {}
    for artifact in artifacts:
        raw = artifact.get("raw_ref")
        if not isinstance(raw, dict) or raw.get("source") != "telegram":
            continue
        locator = raw.get("locator")
        if not isinstance(locator, dict) or not isinstance(raw.get("path"), str):
            continue
        line = locator.get("line")
        if not raw["path"]:
            continue
        if type(line) is not int or line < 1:
            continue
        path = paths.root / raw["path"]
        references.setdefault(path, {}).setdefault(line, []).append(artifact)

    native: set[str] = set()
    modes = {"manual", "stream", "tdlib-once", "tdlib-run", "tdlib-daemon", "pending-offline"}
    for path, lines in references.items():
        last_line = max(lines)
        try:
            with path.open(encoding="utf-8") as stream:
                for number, text in enumerate(stream, 1):
                    if number in lines:
                        try:
                            row = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(row, dict):
                            continue
                        mode = row.get("capture_mode")
                        if row.get("source") != "telegram" or not isinstance(mode, str):
                            continue
                        if mode not in modes:
                            continue
                        payload = row.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        content = _message(payload).get("content")
                        details = _file_details(content) if isinstance(content, dict) else None
                        if details is None:
                            continue
                        _, file, _ = details
                        remote = file.get("remote") or {}
                        if not isinstance(remote, dict):
                            continue
                        file_id = str(file.get("id") or remote.get("id") or f"line-{number}")
                        for artifact in lines[number]:
                            if (
                                str(row.get("account") or "personal") == artifact.get("account")
                                and artifact.get("source_object_id") == file_id
                                and _remote_id(artifact) == (remote.get("id") or None)
                            ):
                                native.add(artifact["artifact_id"])
                    if number >= last_line:
                        break
        except (OSError, UnicodeError):
            continue
    return native


def telegram_artifacts_need_tdlib(paths: RecallPaths, *, date: str, force: bool = False) -> bool:
    artifacts = read_jsonl(paths.artifact_metadata_path("telegram", date))
    native = _native_artifact_ids(paths, artifacts)
    return any(
        artifact["artifact_id"] in native
        and _choose_local_source_path(artifact) is None
        and not (
            not force
            and artifact.get("download_status") == "downloaded"
            and _blob_path(paths, date=date, artifact=artifact).exists()
        )
        for artifact in artifacts
    )


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
    native = (
        _native_artifact_ids(paths, artifacts)
        if policy == "download-source-native" and not dry_run
        else set()
    )
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
        if source_path is None and artifact["artifact_id"] in native:
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
                else (
                    "No Telegram local file; TDLib fallback requires a saved native source record"
                    if artifact["artifact_id"] not in native
                    else (
                        "No Telegram local file, file id, or remote id produced a downloadable file"
                    )
                )
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
