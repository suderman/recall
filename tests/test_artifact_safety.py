from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from recall.connectors.bluebubbles.artifacts import download_bluebubbles_artifacts
from recall.connectors.slack.artifacts import download_slack_artifacts
from recall.connectors.telegram.artifacts import (
    download_telegram_artifacts,
    telegram_artifacts_need_tdlib,
)
from recall.normalize.artifacts import NormalizedArtifact
from recall.normalize.rebuild import _preserve_input_downloads
from recall.storage.jsonl import read_jsonl, write_artifact_metadata, write_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-03-31"
OLD = b"previous good bytes"
NEW = b"replacement bytes"


class HttpClient:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.failure == "http":
            return httpx.Response(403, request=httpx.Request("GET", url))
        return httpx.Response(200, content=NEW, request=httpx.Request("GET", url))


def setup(paths, source, *, imported=False):
    blob = paths.artifacts_blobs / source / "2026" / DAY / "artifact_a--fixture.bin"
    if imported:
        blob = paths.raw / "bluebubbles/imports/bundle/fixture.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(OLD)
    row = {
        "artifact_id": "artifact_a",
        "source": source,
        "kind": "document",
        "account": "personal",
        "source_object_id": "991",
        "event_ids": [],
        "filename": "fixture.bin",
        "local_path": paths.relative_to_root(blob),
        "size_bytes": len(OLD),
        "checksums": {"sha256": hashlib.sha256(OLD).hexdigest()},
        "download_status": "imported" if imported else "downloaded",
        "last_error": None,
        "remote_locators": [{"kind": "url_private_download", "value": "https://fake.invalid/blob"}],
    }
    # Prove native origin for TDLib preflight without initializing TDLib.
    if source == "telegram":
        raw = paths.raw_capture_dir(source, DAY) / "updates.jsonl"
        write_jsonl(
            raw,
            [
                {
                    "source": source,
                    "account": "personal",
                    "capture_mode": "stream",
                    "payload": {
                        "message": {
                            "content": {
                                "@type": "messageDocument",
                                "document": {"document": {"id": 991}},
                            }
                        }
                    },
                }
            ],
        )
        row["raw_ref"] = {
            "source": source,
            "path": paths.relative_to_root(raw),
            "locator": {"line": 1},
        }
    write_jsonl(paths.artifact_metadata_path(source, DAY), [row])
    return blob, row


def download(paths, source, client, **kwargs):
    options = {"date": DAY, "policy": "download-source-native", **kwargs}
    if source == "telegram":
        return download_telegram_artifacts(paths, **options)
    if source == "slack":
        return download_slack_artifacts(paths, token="FAKE_TOKEN", client=client, **options)
    return download_bluebubbles_artifacts(
        paths,
        server_url="https://fake.invalid",
        password="FAKE_PASSWORD +/&",
        client=client,
        **options,
    )


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles"])
@pytest.mark.parametrize("failure", ["read", "write", "replace", "http"])
def test_failed_force_preserves_bytes_and_integrity(tmp_path, monkeypatch, source, failure):
    if source == "telegram" and failure == "http":
        pytest.skip("Telegram acquires local bytes, not HTTP")
    paths = RecallPaths.from_root(tmp_path)
    blob, before = setup(paths, source)
    local = tmp_path / "local.bin"
    local.write_bytes(NEW)
    if source == "telegram":
        before["remote_locators"] = [{"kind": "local_path", "value": str(local)}]
        write_jsonl(paths.artifact_metadata_path(source, DAY), [before])
    original_open = Path.open

    def bad_open(path, mode="r", *args, **kwargs):
        if failure == "read" and path == local and mode == "rb":
            raise OSError("injected local read failure")
        return original_open(path, mode, *args, **kwargs)

    if failure == "read":
        if source == "telegram":
            monkeypatch.setattr(Path, "open", bad_open)
        else:

            def interrupted(_response, *args, **kwargs):
                yield b"partial bytes"
                raise OSError("injected response read failure")

            monkeypatch.setattr(httpx.Response, "iter_bytes", interrupted)
    if failure in {"write", "replace"}:
        import recall.storage.blobs as blobs

        original = blobs.os.fsync if failure == "write" else blobs.os.replace

        def broken(*args):
            if (
                failure == "write" and blob.name in blobs.os.readlink(f"/proc/self/fd/{args[0]}")
            ) or (failure == "replace" and Path(args[1]) == blob):
                raise OSError("injected write/replace failure")
            return original(*args)

        monkeypatch.setattr(blobs.os, "fsync" if failure == "write" else "replace", broken)

    result = download(paths, source, HttpClient(failure), force=True)
    assert result.failed == 1 and result.downloaded == 0
    assert blob.read_bytes() == OLD
    after = read_jsonl(result.artifact_path)[0]
    for field in ("local_path", "checksums", "size_bytes", "download_status"):
        assert after[field] == before[field]
    assert after["last_error"]
    assert "FAKE_PASSWORD" not in after["last_error"]
    assert list(blob.parent.iterdir()) == [blob]


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles"])
def test_successful_force_and_verified_repeat(tmp_path, source):
    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, source)
    local = tmp_path / "local.bin"
    local.write_bytes(NEW)
    if source == "telegram":
        row["remote_locators"] = [{"kind": "local_path", "value": str(local)}]
        write_jsonl(paths.artifact_metadata_path(source, DAY), [row])
    client = HttpClient()
    result = download(paths, source, client, force=True)
    assert result.downloaded == 1 and result.failed == 0
    assert blob.read_bytes() == NEW
    after = read_jsonl(result.artifact_path)[0]
    assert after["size_bytes"] == len(NEW)
    assert after["checksums"] == {"sha256": hashlib.sha256(NEW).hexdigest()}
    calls = client.calls
    repeat = download(paths, source, client)
    assert repeat.skipped_existing == 1 and repeat.failed == 0
    assert client.calls == calls
    assert list(blob.parent.iterdir()) == [blob]


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink"])
def test_telegram_same_file_never_truncates(tmp_path, alias):
    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, "telegram")
    local = blob
    if alias != "same":
        local = tmp_path / "alias.bin"
        if alias == "symlink":
            local.symlink_to(blob)
        else:
            local.hardlink_to(blob)
    row["remote_locators"] = [{"kind": "local_path", "value": str(local)}]
    write_jsonl(paths.artifact_metadata_path("telegram", DAY), [row])
    result = download(paths, "telegram", None, force=True)
    assert result.downloaded == 1 and result.failed == 0
    assert blob.read_bytes() == local.read_bytes() == OLD
    assert read_jsonl(result.artifact_path)[0]["checksums"] == row["checksums"]


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles", "imported"])
@pytest.mark.parametrize("damage", ["missing", "checksum", "size", "tampered", "valid", "no-size"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cache_reuse_checks_bytes_without_repair(tmp_path, source, damage, dry_run):
    paths = RecallPaths.from_root(tmp_path)
    imported = source == "imported"
    source = "bluebubbles" if imported else source
    blob, row = setup(paths, source, imported=imported)
    if damage == "missing":
        blob.unlink()
    elif damage == "checksum":
        row["checksums"] = {}
    elif damage == "size":
        row["size_bytes"] += 1
    elif damage == "tampered":
        blob.write_bytes(b"X" * len(OLD))
    elif damage == "no-size":
        row["size_bytes"] = None
    write_jsonl(paths.artifact_metadata_path(source, DAY), [row])
    before = paths.artifact_metadata_path(source, DAY).read_bytes()
    client = HttpClient()
    if source == "telegram":
        assert not telegram_artifacts_need_tdlib(paths, date=DAY)
    result = download(paths, source, client, dry_run=dry_run)
    valid = damage in {"valid", "no-size"}
    assert result.failed == (not valid)
    assert result.skipped_existing == valid
    assert result.downloaded == result.would_download == client.calls == 0
    if blob.exists():
        assert blob.read_bytes() == (b"X" * len(OLD) if damage == "tampered" else OLD)
    if dry_run:
        assert result.artifact_path.read_bytes() == before
    else:
        after = read_jsonl(result.artifact_path)[0]
        for field in ("local_path", "checksums", "size_bytes", "download_status"):
            assert after[field] == row[field]
        assert bool(after["last_error"]) == (not valid)


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles"])
def test_interruption_cleans_stage_but_keeps_original(tmp_path, monkeypatch, source):
    import recall.storage.blobs as blobs

    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, source)
    if source == "telegram":
        row["remote_locators"] = [{"kind": "local_path", "value": str(blob)}]
        write_jsonl(paths.artifact_metadata_path(source, DAY), [row])
    before = paths.artifact_metadata_path(source, DAY).read_bytes()

    def interrupted(fd):
        raise KeyboardInterrupt("injected interruption after staged write")

    monkeypatch.setattr(blobs.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        download(paths, source, HttpClient(), force=True)
    assert blob.read_bytes() == OLD
    assert paths.artifact_metadata_path(source, DAY).read_bytes() == before
    assert list(blob.parent.iterdir()) == [blob]


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles"])
def test_unrecorded_existing_blob_is_not_implicitly_replaced(tmp_path, source):
    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, source)
    row.update(download_status="not_requested", local_path=None, checksums={})
    write_jsonl(paths.artifact_metadata_path(source, DAY), [row])
    client = HttpClient()
    if source == "telegram":
        assert not telegram_artifacts_need_tdlib(paths, date=DAY)
    result = download(paths, source, client)
    assert result.failed == 1 and result.would_download == client.calls == 0
    assert blob.read_bytes() == OLD
    assert "missing SHA-256" in read_jsonl(result.artifact_path)[0]["last_error"]


@pytest.mark.parametrize("failure", ["http", "exception"])
def test_bluebubbles_persisted_error_has_no_secret(tmp_path, failure):
    from urllib.parse import quote, quote_plus

    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, "bluebubbles", imported=True)
    secret = "FAKE_PASSWORD +/&"

    class Client(HttpClient):
        def get(self, url, **kwargs):
            if failure == "exception":
                raise RuntimeError(f"{url} {secret} {quote(secret)}")
            return super().get(url, **kwargs)

    result = download(paths, "bluebubbles", Client("http"), force=True)
    assert result.failed == 1
    persisted = result.artifact_path.read_text()
    assert not any(value in persisted for value in (secret, quote(secret), quote_plus(secret)))
    after = read_jsonl(result.artifact_path)[0]
    for field in ("local_path", "checksums", "size_bytes", "download_status"):
        assert after[field] == row[field]
    assert blob.read_bytes() == OLD


@pytest.mark.parametrize("imported", [False, True])
def test_normalize_and_rebuild_keep_acquired_integrity_metadata(tmp_path, imported):
    paths = RecallPaths.from_root(tmp_path)
    blob, row = setup(paths, "bluebubbles", imported=imported)
    observation = NormalizedArtifact(
        artifact_id=row["artifact_id"], source="bluebubbles", kind="document", size_bytes=999
    )
    write_artifact_metadata(paths, source="bluebubbles", date=DAY, artifacts=[observation])
    after = read_jsonl(paths.artifact_metadata_path("bluebubbles", DAY))[0]
    assert after["size_bytes"] == len(OLD)
    assert after["checksums"] == row["checksums"]
    replay = observation.to_record()
    _preserve_input_downloads(paths, "bluebubbles", [replay])
    assert replay["size_bytes"] == len(OLD)
    assert replay["checksums"] == row["checksums"]
    assert replay["local_path"] == str(blob)
