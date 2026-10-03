from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.connectors.telegram.artifacts import download_telegram_artifacts
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-03-31"


def message(**fields):
    return {"id": 1, "type": "message", "date": DAY + "T21:00:00Z", "text": "Fixture", **fields}


def export(root, data):
    directory = root / "export"
    directory.mkdir()
    (directory / "result.json").write_text(json.dumps(data))
    return directory


@pytest.mark.parametrize(
    "shape",
    [
        {"id": 5, "name": "Single chat", "messages": [message()]},
        {"chats": {"list": "not a list"}},
        {"chats": {"list": [{"messages": "not a list"}]}},
        {"chats": {"list": [{"messages": [{"type": "service"}]}]}},
        {"unrecognized": "nonempty"},
        [],
    ],
)
def test_unsupported_nonempty_export_never_reports_empty_success(tmp_path, shape):
    paths = RecallPaths.from_root(tmp_path / "archive")
    directory = export(tmp_path, shape)
    before = (directory / "result.json").read_bytes()
    with pytest.raises(ValueError, match="Unsupported|No importable"):
        import_telegram_export(paths, export_path=directory, account="personal")
    assert (directory / "result.json").read_bytes() == before
    assert not list(paths.raw.rglob("result.json"))
    assert not list(paths.raw.rglob("updates.jsonl"))


def test_supported_empty_and_partial_exports_report_skips(tmp_path):
    paths = RecallPaths.from_root(tmp_path / "archive")
    data = {
        "chats": {
            "list": [
                {
                    "id": 5,
                    "messages": [
                        message(),
                        {"type": "service"},
                        message(id=2, date=""),
                        7,
                    ],
                }
            ]
        }
    }
    directory = export(tmp_path, data)
    result = import_telegram_export(paths, export_path=directory, account="personal")
    assert result.messages_imported == 1 and result.messages_skipped == 3
    assert (result.import_dir / "result.json").read_bytes() == (
        directory / "result.json"
    ).read_bytes()
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "result.json").write_text('{"chats":{"list":[]}}')
    empty = import_telegram_export(paths, export_path=empty_dir, account="personal")
    assert empty.messages_imported == empty.messages_skipped == 0
    assert empty.dates_written == []


@pytest.mark.parametrize("path_kind", ["absolute", "parent", "symlink", "directory"])
@pytest.mark.parametrize("media_kind", ["file", "photo"])
def test_import_rejects_media_outside_root_before_read_or_capture(tmp_path, path_kind, media_kind):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"Private outside bytes")
    data = {"chats": {"list": [{"id": 5, "messages": [message()]}]}}
    directory = export(tmp_path, data)
    if path_kind == "absolute":
        locator = str(outside)
    elif path_kind == "parent":
        locator = "../outside.bin"
    elif path_kind == "symlink":
        (directory / "link.bin").symlink_to(outside)
        locator = "link.bin"
    else:
        (directory / "media").mkdir()
        locator = "media"
    data["chats"]["list"][0]["messages"][0][media_kind] = locator
    (directory / "result.json").write_text(json.dumps(data))
    paths = RecallPaths.from_root(tmp_path / "archive")
    with pytest.raises(ValueError, match="media"):
        import_telegram_export(paths, export_path=directory, account="personal")
    assert outside.read_bytes() == b"Private outside bytes"
    assert not list(paths.raw.rglob("updates.jsonl"))
    assert not list(paths.artifacts_blobs.rglob("*.bin"))


def test_relative_media_and_internal_symlink_stay_in_retained_bundle(tmp_path):
    directory = export(
        tmp_path,
        {
            "chats": {
                "list": [
                    {
                        "id": 5,
                        "messages": [
                            message(file="link.bin"),
                            message(id=2, file="missing.bin"),
                        ],
                    }
                ]
            }
        },
    )
    (directory / "bytes.bin").write_bytes(b"Retained fixture bytes")
    (directory / "link.bin").symlink_to("bytes.bin")
    paths = RecallPaths.from_root(tmp_path / "archive")
    result = import_telegram_export(paths, export_path=directory, account="personal")
    assert (result.import_dir / "link.bin").is_symlink()
    normalize_telegram_day(paths, date=DAY)
    downloaded = download_telegram_artifacts(paths, date=DAY, policy="download-source-native")
    assert downloaded.downloaded == 1 and downloaded.failed == 1
    assert (
        read_jsonl(downloaded.artifact_path)[0]["checksums"]
        or read_jsonl(downloaded.artifact_path)[1]["checksums"]
    )


def test_import_cli_reports_partial_records(tmp_path):
    directory = export(
        tmp_path,
        {
            "chats": {
                "list": [
                    {
                        "id": 5,
                        "messages": [
                            message(),
                            {"type": "service"},
                        ],
                    }
                ]
            }
        },
    )
    result = CliRunner().invoke(
        app, ["import", "telegram-export", str(directory), "--root", str(tmp_path / "archive")]
    )
    assert result.exit_code == 0, result.output
    assert "messages_imported=1" in result.output and "messages_skipped=1" in result.output


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_old_import_outside_locator_cannot_acquire_bytes(tmp_path, dry_run, force):
    directory = export(
        tmp_path,
        {
            "chats": {
                "list": [
                    {
                        "id": 5,
                        "messages": [
                            message(file="inside.bin"),
                        ],
                    }
                ]
            }
        },
    )
    (directory / "inside.bin").write_bytes(b"Good export bytes")
    paths = RecallPaths.from_root(tmp_path / "archive")
    import_telegram_export(paths, export_path=directory, account="personal")
    raw = paths.raw_capture_dir("telegram", DAY) / "updates.jsonl"
    rows = read_jsonl(raw)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"Unrelated private bytes")
    rows[0]["payload"]["message"]["content"]["document"]["document"]["local"]["path"] = str(outside)
    write_jsonl(raw, rows)
    normalize_telegram_day(paths, date=DAY)
    artifact_path = paths.artifact_metadata_path("telegram", DAY)
    before = artifact_path.read_bytes()
    result = download_telegram_artifacts(
        paths, date=DAY, policy="download-source-native", dry_run=dry_run, force=force
    )
    assert result.failed == 1 and result.downloaded == result.would_download == 0
    assert not list(paths.artifacts_blobs.rglob("*.bin"))
    assert outside.read_bytes() == b"Unrelated private bytes"
    if dry_run:
        assert artifact_path.read_bytes() == before
    else:
        assert "retained root" in read_jsonl(artifact_path)[0]["last_error"]
