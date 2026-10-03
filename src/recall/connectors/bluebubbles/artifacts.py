from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from recall.connectors.bluebubbles.diagnostics import safe_error
from recall.storage.blobs import cache_error, replace_blob
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class BlueBubblesArtifactHttpClient(Protocol):
    def get(self, url: str) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BlueBubblesArtifactDownloadResult:
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
        / "bluebubbles"
        / date[:4]
        / date
        / f"{artifact['artifact_id']}--{filename}"
    )


def _download_url(server_url: str, password: str, artifact: dict[str, Any]) -> str | None:
    source_object_id = artifact.get("source_object_id")
    if not source_object_id:
        return None
    base = server_url.rstrip("/")
    query = urlencode({"guid": password, "original": "false"})
    return f"{base}/api/v1/attachment/{source_object_id}/download?{query}"


def download_bluebubbles_artifacts(
    paths: RecallPaths,
    *,
    date: str,
    server_url: str,
    password: str,
    policy: str,
    client: BlueBubblesArtifactHttpClient | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> BlueBubblesArtifactDownloadResult:
    artifact_path = paths.artifact_metadata_path("bluebubbles", date)
    if not artifact_path.exists():
        raise FileNotFoundError(f"No artifact metadata file found at {artifact_path}")

    artifacts = read_jsonl(artifact_path)
    would_download = 0
    downloaded = 0
    skipped_policy = 0
    skipped_existing = 0
    failed = 0
    http_client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    owns_client = client is None

    try:
        for artifact in artifacts:
            current_status = artifact.get("download_status") or "not_requested"
            blob_path = _blob_path(paths, date=date, artifact=artifact)
            if (
                current_status in {"downloaded", "imported"}
                or artifact.get("local_path")
                or blob_path.exists()
            ) and not force:
                cached_path = paths.root / (artifact.get("local_path") or str(blob_path))
                error = cache_error(cached_path, artifact)
                artifact["last_error"] = error
                if error:
                    failed += 1
                else:
                    skipped_existing += 1
                continue

            if policy != "download-source-native":
                artifact["download_status"] = (
                    current_status
                    if current_status in {"downloaded", "imported"}
                    else "not_requested"
                )
                artifact["last_error"] = None
                skipped_policy += 1
                continue

            url = _download_url(server_url, password, artifact)
            if url is None:
                if current_status not in {"downloaded", "imported"}:
                    artifact["download_status"] = "not_available"
                artifact["last_error"] = "No BlueBubbles attachment id available for download"
                failed += 1
                continue

            would_download += 1
            if dry_run:
                artifact["last_error"] = None
                continue

            try:
                response = http_client.get(url)
                response.raise_for_status()
                checksum, size = replace_blob(blob_path, response.iter_bytes())
            except Exception as exc:
                failed += 1
                if current_status not in {"downloaded", "imported"}:
                    artifact["download_status"] = "failed"
                artifact["last_error"] = safe_error(exc)
                continue

            artifact["local_path"] = paths.relative_to_root(blob_path)
            artifact["checksums"] = {"sha256": checksum}
            artifact["size_bytes"] = size
            artifact["download_status"] = "downloaded"
            artifact["last_error"] = None
            downloaded += 1
    finally:
        if owns_client:
            http_client.close()

    if not dry_run:
        write_jsonl(artifact_path, artifacts)
    return BlueBubblesArtifactDownloadResult(
        artifacts_seen=len(artifacts),
        would_download=would_download,
        downloaded=downloaded,
        skipped_policy=skipped_policy,
        skipped_existing=skipped_existing,
        failed=failed,
        artifact_path=artifact_path,
    )
