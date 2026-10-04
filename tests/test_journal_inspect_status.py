from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize.journal import inspect_packet, prepare_journal, save_journal

DAY = "2026-04-02"


def fixture(tmp_path, *, multi=True, coverage=True):
    paths = RecallPaths.from_root(tmp_path / "primary")
    raw = paths.raw / "fixture.jsonl"
    write_jsonl(raw, [{"text": "evidence"}])
    write_jsonl(
        paths.normalized_event_path(DAY),
        [
            {
                "event_id": "evt_one",
                "date": DAY,
                "timestamp": DAY + "T12:00:00Z",
                "source": "email",
                "account": "personal",
                "kind": "message",
                "text": "Evidence",
                "raw_ref": {"path": str(raw), "locator": {"line": 1}},
            }
        ],
    )
    manifest = paths.state / "rebuild/manifest.jsonl"
    if coverage:
        write_jsonl(
            manifest,
            [
                {
                    "date": DAY,
                    "source": "email",
                    "status": "success",
                    "options": {"timezone": "UTC", "account": "personal"},
                }
            ],
        )
    extra = RecallPaths.from_root(tmp_path / "overlay")
    extra.normalized.mkdir(parents=True)
    packet = prepare_journal(
        paths, day=DAY, author="Fixture", include_roots=[extra] if multi else []
    )
    return paths, extra, packet


def unchanged(packet):
    return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in packet.iterdir()}


def test_frozen_coverage_counts_and_order_are_visible_and_immutable(tmp_path):
    paths, extra, packet = fixture(tmp_path)
    before = unchanged(packet)
    report = inspect_packet(packet)
    assert report["input_status"] == "unchanged"
    assert report["sources"] == [{"source": "email", "events": 1}]
    email = next(r for r in report["coverage"] if r["source"] == "email")
    assert email["status"] == "success" and email["account"] == "personal"
    assert email["event_count"] == 1
    assert next(r for r in report["coverage"] if r["source"] == "slack")["status"] == "not-queried"
    assert [r["normalized_path"] for r in report["inputs"]] == [
        str(p.normalized_event_path(DAY)) for p in (paths, extra)
    ]
    assert "truth" in report["review_note"] and "complete" in report["review_note"]
    assert inspect_packet(packet) == report and unchanged(packet) == before


@pytest.mark.parametrize(
    "change",
    [
        "normalized-append",
        "normalized-delete",
        "coverage-change",
        "coverage-delete",
        "coverage-add",
        "overlay-add",
        "directory",
    ],
)
def test_detects_input_drift_without_rewriting_packet(tmp_path, change):
    paths, extra, packet = fixture(tmp_path, coverage=change != "coverage-add")
    before = unchanged(packet)
    normalized = paths.normalized_event_path(DAY)
    manifest = paths.state / "rebuild/manifest.jsonl"
    if change == "normalized-append":
        with normalized.open("a") as stream:
            stream.write("\n")
    elif change == "normalized-delete":
        normalized.unlink()
    elif change == "coverage-change":
        manifest.write_text(manifest.read_text().replace("success", "failed!"))
    elif change == "coverage-delete":
        manifest.unlink()
    elif change == "coverage-add":
        write_jsonl(manifest, [])
    elif change == "overlay-add":
        write_jsonl(extra.normalized_event_path(DAY), [])
    else:
        manifest.unlink()
        manifest.mkdir()
    report = inspect_packet(packet)
    assert report["input_status"] == ("unverifiable" if change == "directory" else "changed")
    assert unchanged(packet) == before
    # Frozen success remains frozen success, not replaced by live failed coverage.
    assert next(r for r in report["coverage"] if r["source"] == "email")["status"] == (
        "not-queried" if change == "coverage-add" else "success"
    )


def test_relocation_of_inputs_is_authoritative(tmp_path, monkeypatch):
    paths, extra, packet = fixture(tmp_path)
    moved = tmp_path / "retained"
    shutil.copytree(paths.root, moved)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(paths.root), "new": str(moved)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    paths.normalized_event_path(DAY).write_text("wrong recreated path")
    report = inspect_packet(packet)
    assert report["input_status"] == "unchanged" and not report["unresolved_citations"]
    assert report["inputs"][0]["resolved_normalized_path"].startswith(str(moved))


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("target", ["packet", "revision"])
def test_require_current_is_explicit_not_a_frozen_validity_change(tmp_path, multi, target):
    paths, extra, packet = fixture(tmp_path, multi=multi)
    revision = save_journal(paths, packet_dir=packet, body="Evidence.[fn:evt_one]", model="fake")
    value = packet if target == "packet" else revision
    command = ["journal", "inspect", "--" + target, str(value)]
    runner = CliRunner()
    assert runner.invoke(app, command).exit_code == 0
    expected = "unchanged" if multi else "unverifiable"
    assert json.loads(runner.invoke(app, command).stdout)["input_status"] == expected
    strict = runner.invoke(app, command + ["--require-current"])
    assert strict.exit_code == (0 if multi else 1)
    with paths.normalized_event_path(DAY).open("a") as stream:
        stream.write("\n")
    assert runner.invoke(app, command).exit_code == 0
    assert runner.invoke(app, command + ["--require-current"]).exit_code == 1


@pytest.mark.parametrize("damage", ["events", "prompt", "packet"])
def test_packet_corruption_still_fails_inspection(tmp_path, damage):
    paths, extra, packet = fixture(tmp_path)
    (
        packet / {"events": "events.jsonl", "prompt": "prompt.org", "packet": "packet.json"}[damage]
    ).write_text("broken")
    result = CliRunner().invoke(app, ["journal", "inspect", "--packet", str(packet)])
    assert result.exit_code == 1


def test_unreadable_coverage_is_visible_without_exception_secret(tmp_path, monkeypatch):
    paths, extra, packet = fixture(tmp_path)
    before = unchanged(packet)
    manifest = paths.state / "rebuild/manifest.jsonl"
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == manifest:
            raise PermissionError("FAKE_SECRET")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    report = inspect_packet(packet)
    assert report["input_status"] == "unverifiable"
    assert "FAKE_SECRET" not in json.dumps(report) and unchanged(packet) == before


def test_file_only_mapping_and_absent_overlay_hashes(tmp_path, monkeypatch):
    paths, extra, packet = fixture(tmp_path)
    manifest = paths.state / "rebuild/manifest.jsonl"
    moved = tmp_path / "retained-coverage.jsonl"
    shutil.move(manifest, moved)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps([{"old": str(manifest), "new": str(moved)}]))
    monkeypatch.setenv("RECALL_RELOCATION_MAP", str(mapping))
    report = inspect_packet(packet)
    assert report["input_status"] == "unchanged"
    assert report["inputs"][1]["normalized_status"] == "absent"
    assert report["inputs"][0]["resolved_coverage_path"] == str(moved)


@pytest.mark.parametrize("damage", ["checksum", "entry", "empty"])
def test_unrecorded_or_malformed_hashes_cannot_certify_currentness(tmp_path, damage):
    from recall.synthesize.journal import _inspect_inputs

    paths, extra, packet = fixture(tmp_path)
    metadata = json.loads((packet / "packet.json").read_text())
    if damage == "checksum":
        metadata["normalized_inputs"][0]["coverage_sha256"] = "FAKE_SECRET"
    elif damage == "entry":
        metadata["normalized_inputs"] = [42]
    else:
        metadata["normalized_inputs"] = []
    report, state = _inspect_inputs(metadata)
    assert state == "unverifiable" and "FAKE_SECRET" not in json.dumps(report)


def test_new_unrelated_day_does_not_make_daily_packet_stale(tmp_path):
    paths, extra, packet = fixture(tmp_path)
    write_jsonl(paths.normalized_event_path("2026-04-03"), [])
    assert inspect_packet(packet)["input_status"] == "unchanged"
