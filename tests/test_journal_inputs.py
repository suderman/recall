import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_journals import DAY, TZ, runner_env, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize import generate, journal


def replay(root: Path, rows: list[dict], *, account=None, status="success") -> RecallPaths:
    paths = RecallPaths.from_root(root)
    write_jsonl(paths.normalized_event_path(DAY), rows)
    write_jsonl(
        paths.state / "rebuild/manifest.jsonl",
        [
            {
                "date": DAY,
                "source": "email",
                "status": status,
                "options": {"timezone": TZ, "account": account},
            }
        ],
    )
    return paths


def snapshot(paths: RecallPaths) -> dict[str, bytes]:
    return {str(file): file.read_bytes() for file in paths.root.rglob("*") if file.is_file()}


def test_ordered_overlays_freeze_physical_origins_and_raw_links(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "primary")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    relative_raw = "data/raw/email/messages.jsonl"
    rows[0]["raw_ref"] = {"path": relative_raw, "locator": {"line": 1}}
    first = replay(tmp_path / "first", [rows[0]])
    rows[0]["text"] = "Updated evidence"
    later = replay(tmp_path / "later", [rows[0]])
    # The winning record is at physical line 3, not nonblank record 1.
    source = later.normalized_event_path(DAY)
    source.write_text("\n\n" + source.read_text())
    for root in (first, later):
        raw = root.root / relative_raw
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"root": str(root.root)}) + "\n")
    before = {str(file): file.read_bytes() for file in paths.normalized.rglob("*.jsonl")}
    included_before = [snapshot(root) for root in (first, later)]
    kwargs: dict[str, Any] = dict(
        day=DAY, author="Example", timezone_name=TZ, include_roots=[first, later]
    )
    packet = journal.prepare_journal(paths, **kwargs)
    assert journal.prepare_journal(paths, **kwargs) == packet
    metadata, events = journal._load_packet(packet)
    assert len(events) == 2
    assert events[1]["text"] == "Updated evidence"
    assert metadata["event_origins"]["evt_mail"] == {"normalized_path": str(source), "line": 3}
    assert metadata["event_origins"]["evt_break"]["normalized_path"] == str(
        paths.normalized_event_path(DAY)
    )
    for record in metadata["normalized_inputs"]:
        assert (
            record["normalized_sha256"]
            == hashlib.sha256(Path(record["normalized_path"]).read_bytes()).hexdigest()
        )
        assert (
            record["coverage_sha256"]
            == hashlib.sha256(Path(record["coverage_path"]).read_bytes()).hexdigest()
        )
    revision = journal.save_journal(
        paths, packet_dir=packet, body="Prepared.[fn:evt_mail]", model="test"
    )
    content = revision.read_text()
    assert f"{source}::3" in content
    assert str(later.root / relative_raw) in content
    assert str(first.root / relative_raw) not in content
    assert [snapshot(root) for root in (first, later)] == included_before
    assert {str(file): file.read_bytes() for file in paths.normalized.rglob("*.jsonl")} == before
    reversed_packet = journal.prepare_journal(paths, **{**kwargs, "include_roots": [later, first]})
    assert reversed_packet != packet
    assert journal._load_packet(reversed_packet)[1][1]["text"] != "Updated evidence"


@pytest.mark.parametrize("status", ["success", "failed", "queried-empty"])
def test_coverage_is_account_scoped_and_empty_does_not_delete(tmp_path: Path, status: str) -> None:
    paths = workspace(tmp_path / "primary")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["account"] = "personal"
    write_jsonl(paths.normalized_event_path(DAY), rows)
    write_jsonl(paths.state / "rebuild/manifest.jsonl", [])
    work = {**rows[0], "event_id": "evt_work", "account": "work"}
    other = replay(tmp_path / "other", [work], account="work")
    latest = replay(tmp_path / "latest", [], account="work", status=status)
    packet = journal.prepare_journal(
        paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other, latest]
    )
    metadata, events = journal._load_packet(packet)
    assert {row["event_id"] for row in events} == {"evt_mail", "evt_work", "evt_break"}
    coverage = next(row for row in metadata["coverage"] if row["source"] == "email")
    assert coverage["status"] == "partial" and coverage["event_count"] == 2
    scopes = {row["account"]: row["status"] for row in coverage["scopes"]}
    assert scopes == {
        "personal": "not-queried",
        "work": "partial" if status == "queried-empty" else status,
    }


def test_new_evidence_without_coverage_is_not_certified_by_old_root(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "primary")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    rows[0]["text"] = "New, unverified input"
    other = replay(tmp_path / "other", [rows[0]])
    (other.state / "rebuild/manifest.jsonl").unlink()
    packet = journal.prepare_journal(
        paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other]
    )
    metadata, _ = journal._load_packet(packet)
    coverage = next(row for row in metadata["coverage"] if row["source"] == "email")
    assert coverage["status"] == "not-queried"


def test_input_changed_mid_read_refuses_packet(tmp_path: Path, monkeypatch) -> None:
    paths = workspace(tmp_path / "primary")
    other = replay(tmp_path / "other", read_jsonl(paths.normalized_event_path(DAY)))
    original = journal._json
    changed = False

    def mutate(value):
        nonlocal changed
        if not changed:
            changed = True
            with other.normalized_event_path(DAY).open("a") as handle:
                handle.write("\n")
        return original(value)

    monkeypatch.setattr(journal, "_json", mutate)
    with pytest.raises(ValueError, match="input changed"):
        journal.prepare_journal(
            paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other]
        )
    assert not (paths.derived / "journal-inputs").exists()


def test_missing_day_and_zero_event_coverage(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "primary")
    other = replay(tmp_path / "other", [], account="work", status="queried-empty")
    other.normalized_event_path(DAY).unlink()
    # Missing files are recorded, but do not erase the primary day's evidence.
    packet = journal.prepare_journal(
        paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other]
    )
    metadata, events = journal._load_packet(packet)
    assert len(events) == 2 and metadata["normalized_inputs"][1]["normalized_sha256"] is None
    assert (
        next(row for row in metadata["coverage"] if row["source"] == "email")["status"] == "success"
    )
    paths.normalized_event_path(DAY).unlink()
    # Evidence only in included roots is valid; genuinely empty days still fail.
    with pytest.raises(ValueError, match="No evidence"):
        journal.prepare_journal(
            paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other]
        )
    replay(other.root, events)
    packet = journal.prepare_journal(
        paths, day=DAY, author="Example", timezone_name=TZ, include_roots=[other]
    )
    assert journal._load_packet(packet)[0]["normalized_sha256"] is None


@pytest.mark.parametrize(
    "fault", ["duplicates", "json", "timezone", "coverage", "busy", "same_root", "missing_root"]
)
def test_reject_invalid_or_busy_replays(tmp_path: Path, fault: str) -> None:
    paths = workspace(tmp_path / "primary")
    rows = read_jsonl(paths.normalized_event_path(DAY))
    other = replay(tmp_path / "other", rows)
    args: dict[str, Any] = dict(day=DAY, author="Example", timezone_name=TZ, include_roots=[other])
    source = other.normalized_event_path(DAY)
    if fault == "duplicates":
        write_jsonl(source, rows + rows[:1])
    elif fault == "json":
        source.write_text("\nnot json\n")
    elif fault == "timezone":
        jobs = read_jsonl(other.state / "rebuild/manifest.jsonl")
        jobs[0]["options"]["timezone"] = "UTC"
        write_jsonl(other.state / "rebuild/manifest.jsonl", jobs)
    elif fault == "coverage":
        (other.state / "rebuild/manifest.jsonl").write_text("null\n")
    elif fault == "same_root":
        args["include_roots"] = [paths]
    elif fault == "missing_root":
        args["include_roots"] = [RecallPaths.from_root(tmp_path / "absent")]
    lock = other.state / "rebuild/writer.lock"
    with lock.open("a") as handle:
        if fault == "busy":
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = snapshot(other)
        with pytest.raises((ValueError, BlockingIOError)):
            journal.prepare_journal(paths, **args)
        assert snapshot(other) == before
    assert not (paths.derived / "journal-inputs").exists()


def test_cli_build_uses_overlay_cache_and_protects_manual_edits(
    tmp_path: Path, monkeypatch
) -> None:
    paths = workspace(tmp_path / "primary")
    row = read_jsonl(paths.normalized_event_path(DAY))[0]
    row["text"] = "Extra replay evidence"
    other = replay(tmp_path / "other", [row])
    calls = []

    def model(prompt, selected, policy, budget):
        assert "Extra replay evidence" in prompt
        calls.append(1)
        return "Prepared.[fn:evt_mail]", {}

    monkeypatch.setattr(generate, "run_pi", model)
    cli = CliRunner(env=runner_env(tmp_path))
    common = [
        "--root",
        str(paths.root),
        "--author",
        "Example",
        "--timezone",
        TZ,
        "--include-root",
        str(other.root),
    ]
    prepared = cli.invoke(app, ["journal", "prepare", "--date", DAY, *common])
    assert prepared.exit_code == 0, prepared.output
    packet = Path(prepared.output.strip())
    output = tmp_path / "published"
    args = ["journal", "build", "--from", DAY, "--to", DAY, "--output", str(output), *common]
    first = cli.invoke(app, args)
    assert first.exit_code == 0 and "generated" in first.output, first.output
    assert cli.invoke(app, args).output.count("cached") == 1 and len(calls) == 1
    row["text"] += " Changed."
    write_jsonl(other.normalized_event_path(DAY), [row])
    assert cli.invoke(app, args).output.count("generated") == 1 and len(calls) == 2
    assert (
        json.loads((packet / "events.jsonl").read_text().splitlines()[1])["text"]
        == "Extra replay evidence"
    )
    target = output / "2026/03" / f"{DAY}.org"
    target.write_text("Handwritten addition")
    refused = cli.invoke(app, args)
    assert refused.exit_code == 1 and "handwritten/edited" in refused.output
    assert len(calls) == 2 and target.read_text() == "Handwritten addition"
