from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.connectors.telegram.artifacts import download_telegram_artifacts
from recall.connectors.telegram.capture import append_telegram_update
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-03-31"


class FakeClient:
    def __init__(self, file: Path, *, numeric_available: bool = True) -> None:
        self.file = file
        self.numeric_available = numeric_available
        self.requests: list[int] = []
        self.remote_requests: list[tuple[str, str]] = []
        self.closed = False

    def download_file(self, file_id: int, *, timeout_seconds: float = 120.0) -> dict | None:
        self.requests.append(file_id)
        return {"local": {"path": str(self.file)}} if self.numeric_available else None

    def download_remote_file(
        self, remote_id: str, *, kind: str, timeout_seconds: float = 120.0
    ) -> dict:
        self.remote_requests.append((remote_id, kind))
        return {"local": {"path": str(self.file)}}

    def close(self) -> None:
        self.closed = True


def imported_media(root: Path) -> RecallPaths:
    paths = RecallPaths.from_root(root / "archive")
    bundle = root / "export"
    (bundle / "files").mkdir(parents=True)
    (bundle / "files/document.bin").write_bytes(b"export bytes")
    (bundle / "result.json").write_text(
        json.dumps(
            {
                "chats": {
                    "list": [
                        {
                            "id": 5,
                            "messages": [
                                {
                                    "type": "message",
                                    "id": 1048576,
                                    "date": "2026-03-31T21:00:00Z",
                                    "date_unixtime": "1774990800",
                                    "file": "files/document.bin",
                                    "text": "Export fixture",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    import_telegram_export(paths, export_path=bundle, account="personal")
    normalize_telegram_day(paths, date=DAY)
    return paths


def add_native(paths: RecallPaths) -> None:
    append_telegram_update(
        paths,
        account="personal",
        received_at="2026-03-31T21:00:00Z",
        capture_mode="stream",
        update_type="updateNewMessage",
        payload={
            "message": {
                "chat_id": 5,
                "id": 1048576,
                "date": 1774990800,
                "content": {
                    "@type": "messageDocument",
                    "document": {
                        "file_name": "native.bin",
                        "document": {
                            "id": 991,
                            "remote": {"id": "native-remote"},
                        },
                    },
                },
            }
        },
    )
    normalize_telegram_day(paths, date=DAY)


def remove_import_file(paths: RecallPaths) -> None:
    artifacts = read_jsonl(paths.artifact_metadata_path("telegram", DAY))
    for locator in artifacts[0]["remote_locators"]:
        if locator["kind"] == "local_path":
            Path(locator["value"]).unlink()
    artifacts[0]["remote_locators"].append({"kind": "remote_id", "value": "export-key"})
    write_jsonl(paths.artifact_metadata_path("telegram", DAY), artifacts)


@pytest.mark.parametrize("numeric_available", [True, False])
def test_missing_export_media_never_uses_tdlib(tmp_path, numeric_available) -> None:
    paths = imported_media(tmp_path)
    remove_import_file(paths)
    wrong_file = tmp_path / "unrelated.bin"
    wrong_file.write_bytes(b"unrelated TDLib bytes")
    client = FakeClient(wrong_file, numeric_available=numeric_available)

    result = download_telegram_artifacts(
        paths, date=DAY, policy="download-source-native", client=client
    )

    assert client.requests == [] and client.remote_requests == []
    assert result.failed == 1 and result.downloaded == 0
    artifact = read_jsonl(result.artifact_path)[0]
    assert artifact["download_status"] == "not_available"
    assert artifact["checksums"] == {} and artifact["local_path"] is None
    assert "native source" in artifact["last_error"]
    assert not list(paths.artifacts_blobs.rglob("*.bin"))


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_import_only_cli_does_not_initialize_tdlib(tmp_path, monkeypatch, missing, dry_run) -> None:
    import recall.cli.artifacts as cli

    paths = imported_media(tmp_path)
    if missing:
        remove_import_file(paths)
    before = paths.artifact_metadata_path("telegram", DAY).read_bytes()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("initialization")
        raise AssertionError("export-only command must not initialize TDLib")

    monkeypatch.setattr(cli, "build_tdlib_auth_settings", forbidden)
    monkeypatch.setattr(cli, "TdlibJsonTransport", forbidden)
    args = [
        "artifacts",
        "download",
        "telegram",
        "--root",
        str(paths.root),
        "--date",
        DAY,
        "--policy",
        "download-source-native",
    ]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.stdout
    assert calls == []
    if dry_run:
        assert paths.artifact_metadata_path("telegram", DAY).read_bytes() == before
    else:
        artifact = read_jsonl(paths.artifact_metadata_path("telegram", DAY))[0]
        assert artifact["download_status"] == ("not_available" if missing else "downloaded")
        if not missing:
            assert (paths.root / artifact["local_path"]).read_bytes() == b"export bytes"
            assert artifact["checksums"] == {"sha256": hashlib.sha256(b"export bytes").hexdigest()}


@pytest.mark.parametrize("numeric_available", [False, True])
def test_mixed_cli_only_downloads_native_ids_and_closes_client(
    tmp_path, monkeypatch, numeric_available
) -> None:
    import recall.cli.artifacts as cli

    paths = imported_media(tmp_path)
    remove_import_file(paths)
    add_native(paths)
    file = tmp_path / "native-bytes.bin"
    file.write_bytes(b"native bytes")
    clients = []
    setup_calls = []

    def settings(*args):
        setup_calls.append("settings")
        return SimpleNamespace(library_path=None, log_verbosity_level=0)

    def client(**kwargs):
        value = FakeClient(file, numeric_available=numeric_available)
        clients.append(value)
        return value

    monkeypatch.setattr(cli, "build_tdlib_auth_settings", settings)
    monkeypatch.setattr(cli, "TdlibJsonTransport", lambda **kwargs: object())
    monkeypatch.setattr(cli, "TdlibTelegramClient", client)
    args = [
        "artifacts",
        "download",
        "telegram",
        "--root",
        str(paths.root),
        "--date",
        DAY,
        "--policy",
        "download-source-native",
    ]
    for flags in ([], [], ["--force"]):
        result = CliRunner().invoke(app, args + flags)
        assert result.exit_code == 0, result.stdout
        assert "failed=1" in result.stdout
    assert len(clients) == len(setup_calls) == 2
    for value in clients:
        assert value.closed
        assert value.requests == [991]
        assert value.remote_requests == (
            [] if numeric_available else [("native-remote", "document")]
        )
    artifacts = read_jsonl(paths.artifact_metadata_path("telegram", DAY))
    assert sorted(a["download_status"] for a in artifacts) == ["downloaded", "not_available"]
    native = next(a for a in artifacts if a["download_status"] == "downloaded")
    assert (paths.root / native["local_path"]).read_bytes() == b"native bytes"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_ref",
        "missing_file",
        "out_of_range",
        "bad_json",
        "wrong_account",
        "unknown_mode",
        "wrong_file_key",
        "context_record",
    ],
)
def test_unproved_source_refuses_tdlib(tmp_path, damage) -> None:
    paths = imported_media(tmp_path)
    add_native(paths)
    artifacts = read_jsonl(paths.artifact_metadata_path("telegram", DAY))
    native = next(a for a in artifacts if a["source_object_id"] == "991")
    raw_path = paths.root / native["raw_ref"]["path"]
    if damage == "missing_ref":
        native["raw_ref"] = None
    elif damage == "missing_file":
        raw_path.unlink()
    elif damage == "out_of_range":
        native["raw_ref"]["locator"]["line"] = 100
    elif damage == "bad_json":
        raw_path.write_text("\nnot JSON\n")
    else:
        rows = read_jsonl(raw_path)
        if damage == "wrong_file_key":
            rows[1]["payload"]["message"]["content"]["document"]["document"]["id"] = 992
        elif damage == "context_record":
            rows[1]["payload"] = {"@type": "updateUser", "user": {"id": 991}}
        else:
            rows[1]["account" if damage == "wrong_account" else "capture_mode"] = "unknown"
        write_jsonl(raw_path, rows)
    write_jsonl(paths.artifact_metadata_path("telegram", DAY), [native])
    file = tmp_path / "unrelated.bin"
    file.write_bytes(b"wrong bytes")
    client = FakeClient(file)
    result = download_telegram_artifacts(
        paths, date=DAY, policy="download-source-native", client=client
    )
    assert result.failed == 1 and result.downloaded == 0
    assert client.requests == [] and client.remote_requests == []
