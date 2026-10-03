from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths

DAY = "2026-04-02"
DATA = b"acquired evidence"
runner = CliRunner()


def record(tmp_path, source="telegram", **changes):
    paths = RecallPaths.from_root(tmp_path)
    blob = paths.artifacts_blobs / source / "fixture.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(DATA)
    row = {
        "artifact_id": "artifact_fixture",
        "source": source,
        "download_status": "downloaded",
        "local_path": paths.relative_to_root(blob),
        "size_bytes": len(DATA),
        "checksums": {"sha256": hashlib.sha256(DATA).hexdigest()},
        "last_error": "FAKE_SECRET in historic diagnostics",
        "remote_locators": [{"kind": "url", "value": "https://fake.invalid/?guid=FAKE_SECRET"}],
        **changes,
    }
    metadata = paths.artifact_metadata_path(source, DAY)
    write_jsonl(metadata, [row])
    return paths, blob, row


def verify(paths, source="telegram", *, as_json=True):
    return runner.invoke(
        app,
        [
            "artifacts",
            "verify",
            "--root",
            str(paths.root),
            "--source",
            source,
            "--date",
            DAY,
            *(["--json"] if as_json else []),
        ],
    )


@pytest.mark.parametrize("source", ["telegram", "slack", "bluebubbles"])
def test_offline_verify_all_connectors_without_credentials_or_clients(
    tmp_path, monkeypatch, source
):
    import recall.cli.artifacts as cli

    paths, blob, row = record(tmp_path, source)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (blob, paths.artifact_metadata_path(source, DAY))
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("verification must not load configuration or start acquisition")

    for name in [
        "load_dotenv",
        "load_slack_config",
        "load_bluebubbles_config",
        "load_telegram_config",
        "TdlibJsonTransport",
        "download_slack_artifacts",
        "download_bluebubbles_artifacts",
        "download_telegram_artifacts",
    ]:
        monkeypatch.setattr(cli, name, forbidden)
    result = verify(paths, source)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["counts"] == {"valid": 1, "invalid": 0, "unverifiable": 0, "not_acquired": 0}
    assert report["artifacts"][0]["verification"] == "valid"
    assert "FAKE_SECRET" not in result.output
    assert not paths.database.exists()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


@pytest.mark.parametrize(
    "damage,error",
    [
        ("missing", "bytes missing"),
        ("no-path", "missing local_path"),
        ("checksum", "missing SHA-256"),
        ("size", "size mismatch"),
        ("hash", "SHA-256 mismatch"),
        ("unreadable", "unreadable"),
    ],
)
def test_verify_errors_do_not_repair_or_delete(tmp_path, monkeypatch, damage, error):
    paths, blob, row = record(tmp_path)
    if damage == "missing":
        blob.unlink()
    elif damage == "no-path":
        row["local_path"] = None
    elif damage == "checksum":
        row["checksums"] = {}
    elif damage == "size":
        row["size_bytes"] += 1
    elif damage == "hash":
        blob.write_bytes(b"X" * len(DATA))
    elif damage == "unreadable":
        original_open = Path.open

        def fail(path, *args, **kwargs):
            if path == blob:
                raise PermissionError("FAKE_SECRET")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail)
    metadata = paths.artifact_metadata_path("telegram", DAY)
    write_jsonl(metadata, [row])
    before = metadata.read_bytes()
    result = verify(paths)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    item = report["artifacts"][0]
    assert item["verification"] == ("unverifiable" if damage == "checksum" else "invalid")
    assert error in item["error"]
    assert metadata.read_bytes() == before
    assert "FAKE_SECRET" not in result.output
    assert blob.exists() == (damage != "missing")


def test_imported_bytes_absolute_relative_and_explicit_relocation(tmp_path, monkeypatch):
    paths, blob, row = record(tmp_path / "archive", "bluebubbles", download_status="imported")
    retained = tmp_path / "retained/import.bin"
    retained.parent.mkdir()
    blob.rename(retained)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(blob), "new": str(retained)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    for local_path in (paths.relative_to_root(blob), str(blob)):
        row["local_path"] = local_path
        metadata = paths.artifact_metadata_path("bluebubbles", DAY)
        write_jsonl(metadata, [row])
        before = metadata.read_bytes()
        result = verify(paths, "bluebubbles")
        assert result.exit_code == 0, result.output
        item = json.loads(result.stdout)["artifacts"][0]
        assert item["resolved_local_path"] == str(retained)
        assert metadata.read_bytes() == before
        assert retained.read_bytes() == DATA


def test_missing_size_is_sha_verified_not_complete_source_proof(tmp_path):
    paths, _, _ = record(tmp_path, size_bytes=None)
    result = verify(paths, as_json=False)
    assert result.exit_code == 0, result.output
    assert "valid=1" in result.output
    assert "does not prove source completeness" in result.output


def test_unacquired_status_is_not_a_valid_cache(tmp_path):
    paths, _, row = record(tmp_path, download_status="failed", local_path=None, checksums={})
    pending = {**row, "artifact_id": "artifact_pending", "download_status": "not_requested"}
    write_jsonl(paths.artifact_metadata_path("telegram", DAY), [row, pending])
    result = verify(paths)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["counts"]["not_acquired"] == 2
    assert report["counts"]["valid"] == 0


@pytest.mark.parametrize(
    "row",
    [
        [],
        {"artifact_id": None},
        {"artifact_id": "a", "source": "slack"},
        {"local_path": ["FAKE_SECRET"]},
        {"size_bytes": True},
        {"checksums": "FAKE_SECRET"},
        {"checksums": {"sha256": ["FAKE_SECRET"]}},
        {"checksums": {"sha256": "not-a-hash"}},
        {"size_bytes": -1},
        {"download_status": "FAKE_SECRET"},
    ],
)
def test_invalid_metadata_is_visible_without_echoing_payload(tmp_path, row):
    paths, _, original = record(tmp_path)
    bad = {**original, **row} if isinstance(row, dict) else row
    metadata = paths.artifact_metadata_path("telegram", DAY)
    write_jsonl(metadata, [bad, original])
    before = metadata.read_bytes()
    result = verify(paths)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["errors"][0]["line"] == 1
    assert report["counts"]["valid"] == 1
    assert "FAKE_SECRET" not in result.output
    assert metadata.read_bytes() == before


def test_broken_json_is_visible_but_other_rows_are_checked(tmp_path):
    paths, _, row = record(tmp_path)
    metadata = paths.artifact_metadata_path("telegram", DAY)
    metadata.write_text('{"password":"FAKE_SECRET",\n' + json.dumps(row) + "\n")
    before = metadata.read_bytes()
    result = verify(paths)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert len(report["errors"]) == report["counts"]["valid"] == 1
    assert "FAKE_SECRET" not in result.output
    assert metadata.read_bytes() == before


@pytest.mark.parametrize(
    "source,day",
    [("../private", DAY), ("telegram", "2026-99-99"), ("telegram", "20260402"), ("/private", DAY)],
)
def test_scope_validation_does_not_create_store(tmp_path, source, day):
    root = tmp_path / "absent"
    result = runner.invoke(
        app,
        ["artifacts", "verify", "--root", str(root), "--source", source, "--date", day, "--json"],
    )
    assert result.exit_code != 0
    assert not root.exists()


def test_absent_metadata_is_not_empty_success(tmp_path):
    root = tmp_path / "absent"
    result = verify(RecallPaths.from_root(root))
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["errors"][0]["kind"] == "metadata_unreadable"
    assert not root.exists()


def test_invalid_relocation_map_is_unverifiable_not_a_fallback(tmp_path, monkeypatch):
    paths, blob, _ = record(tmp_path)
    mapping = tmp_path / "invalid-map.json"
    mapping.write_text('{"password":"FAKE_SECRET"}')
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    result = verify(paths)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["counts"]["unverifiable"] == 1
    assert report["artifacts"][0]["resolved_local_path"] is None
    assert "FAKE_SECRET" not in result.output
    assert blob.read_bytes() == DATA


def test_empty_metadata_is_explicitly_zero_not_acquired_success(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    write_jsonl(paths.artifact_metadata_path("telegram", DAY), [])
    result = verify(paths)
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert not report["artifacts"] and not report["errors"]
    assert all(count == 0 for count in report["counts"].values())


def test_unicode_and_control_characters_are_escaped_in_text(tmp_path):
    paths, _, _ = record(tmp_path, artifact_id="artifact_é\n\x1b[31m")
    result = verify(paths, as_json=False)
    assert result.exit_code == 0
    assert "artifact_\\u00e9\\n\\u001b[31m" in result.output
    assert "\x1b[31m" not in result.output
