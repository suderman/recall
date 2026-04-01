from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

DOWNLOADABLE_LOCATOR_KINDS = ("url_private_download", "url_private")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class SlackArtifactHttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SlackArtifactDownloadResult:
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


def _choose_download_locator(artifact: dict[str, Any]) -> str | None:
    for kind in DOWNLOADABLE_LOCATOR_KINDS:
        for locator in artifact.get("remote_locators", []):
            if locator.get("kind") == kind and locator.get("value"):
                return str(locator["value"])
    return None


def _blob_path(paths: RecallPaths, *, source: str, date: str, artifact: dict[str, Any]) -> Path:
    filename = _sanitize_filename(artifact.get("filename"), str(artifact["artifact_id"]))
    return (
        paths.artifacts_blobs / source / date[:4] / date / f"{artifact['artifact_id']}--{filename}"
    )


def download_slack_artifacts(
    paths: RecallPaths,
    *,
    date: str,
    token: str,
    policy: str,
    client: SlackArtifactHttpClient | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> SlackArtifactDownloadResult:
    artifact_path = paths.artifact_metadata_path("slack", date)
    if not artifact_path.exists():
        raise FileNotFoundError(f"No artifact metadata file found at {artifact_path}")

    artifacts = read_jsonl(artifact_path)
    would_download = 0
    downloaded = 0
    skipped_policy = 0
    skipped_existing = 0
    failed = 0
    http_client = client or httpx.Client(timeout=60.0)
    owns_client = client is None

    try:
        for artifact in artifacts:
            artifact["download_status"] = artifact.get("download_status") or "not_requested"
            locator = _choose_download_locator(artifact)
            if policy != "download-source-native":
                artifact["download_status"] = "not_requested"
                artifact["last_error"] = None
                skipped_policy += 1
                continue

            if locator is None:
                artifact["download_status"] = "not_available"
                artifact["last_error"] = "No source-native downloadable locator found"
                failed += 1
                continue

            blob_path = _blob_path(paths, source="slack", date=date, artifact=artifact)
            if blob_path.exists() and artifact.get("download_status") == "downloaded" and not force:
                artifact["local_path"] = paths.relative_to_root(blob_path)
                artifact["last_error"] = None
                skipped_existing += 1
                continue

            would_download += 1
            if dry_run:
                artifact["last_error"] = None
                continue

            blob_path.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()

            try:
                response = http_client.get(
                    locator,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                with blob_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        checksum.update(chunk)
            except Exception as exc:
                failed += 1
                artifact["download_status"] = "failed"
                artifact["last_error"] = str(exc)
                if blob_path.exists():
                    blob_path.unlink()
                continue

            artifact["local_path"] = paths.relative_to_root(blob_path)
            artifact["checksums"] = {"sha256": checksum.hexdigest()}
            artifact["download_status"] = "downloaded"
            artifact["last_error"] = None
            downloaded += 1
    finally:
        if owns_client:
            http_client.close()

    if not dry_run:
        write_jsonl(artifact_path, artifacts)
    return SlackArtifactDownloadResult(
        artifacts_seen=len(artifacts),
        would_download=would_download,
        downloaded=downloaded,
        skipped_policy=skipped_policy,
        skipped_existing=skipped_existing,
        failed=failed,
        artifact_path=artifact_path,
    )
