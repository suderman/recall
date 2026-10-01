import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_journals import DAY, TZ, workspace

from recall.storage.jsonl import write_jsonl
from recall.synthesize import generate


def test_resume_regeneration_and_manual_edits(tmp_path: Path, monkeypatch) -> None:
    paths = workspace(tmp_path / "recall")
    output = tmp_path / "journal"
    calls = []

    def model(prompt, selected):
        calls.append((prompt, selected))
        return f"** Work\nI prepared the update, revision {len(calls)}.[fn:evt_mail]", {}

    monkeypatch.setattr(generate, "run_pi", model)
    kwargs: dict[str, Any] = dict(
        first=DAY, last=DAY, author="Example", output=output, timezone_name=TZ
    )
    first = generate.build_journals(paths, **kwargs)
    target = output / "2026/03/2026-03-30.org"
    old = target.read_bytes()
    assert first[0]["status"] == "generated"
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "cached"
    assert len(calls) == 1 and target.read_bytes() == old
    assert "evt_mail" in calls[0][0] and "evt_break" in calls[0][0]
    assert calls[0][1] == "codex-lb/gpt-6.1-sol:medium"
    assert generate.build_journals(paths, regenerate=True, **kwargs)[0]["status"] == "generated"
    assert target.read_bytes() != old
    assert len(list((paths.derived / "journals/2026" / DAY).glob("*/journal.org"))) == 2
    target.write_text("My own notes")
    with pytest.raises(ValueError, match="handwritten/edited"):
        generate.build_journals(paths, regenerate=True, **kwargs)
    assert len(calls) == 2 and target.read_text() == "My own notes"


def test_interruption_and_publication_crash_resume(tmp_path: Path, monkeypatch) -> None:
    paths = workspace(tmp_path / "recall")
    output = tmp_path / "journal"
    kwargs: dict[str, Any] = dict(
        first=DAY, last=DAY, author="Example", output=output, timezone_name=TZ
    )
    calls = []

    def model(*args):
        calls.append(1)
        return "Prepared.[fn:evt_mail]", {}

    original_write = generate.write_jsonl

    def interrupt(path, rows):
        if path.name == "journal-publications.jsonl":
            raise KeyboardInterrupt()
        return original_write(path, rows)

    monkeypatch.setattr(generate, "run_pi", model)
    monkeypatch.setattr(generate, "write_jsonl", interrupt)
    with pytest.raises(KeyboardInterrupt):
        generate.build_journals(paths, **kwargs)
    monkeypatch.setattr(generate, "write_jsonl", original_write)
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "cached"
    assert len(calls) == 1
    # Invalid output cannot replace a successful visible journal or checkpoint.
    previous = (output / "2026/03/2026-03-30.org").read_bytes()
    monkeypatch.setattr(generate, "run_pi", lambda *args: ("Unsupported.[fn:unknown]", {}))
    with pytest.raises(ValueError, match="outside its evidence"):
        generate.build_journals(paths, regenerate=True, **kwargs)
    assert (output / "2026/03/2026-03-30.org").read_bytes() == previous
    rejected = list((paths.derived / "journal-failures" / DAY).glob("*/body.txt"))
    assert len(rejected) == 1 and rejected[0].read_text() == "Unsupported.[fn:unknown]"
    assert "unknown" in json.loads((rejected[0].parent / "generation.json").read_text())["error"]


def test_changed_evidence_regenerates_and_busy_writer(tmp_path: Path, monkeypatch) -> None:
    import fcntl

    paths = workspace(tmp_path / "recall")
    kwargs: dict[str, Any] = dict(
        first=DAY, last=DAY, author="Example", output=tmp_path / "out", timezone_name=TZ
    )
    calls = []

    def model(*args):
        calls.append(1)
        return "Prepared.[fn:evt_mail]", {"attempt": len(calls)}

    monkeypatch.setattr(generate, "run_pi", model)
    generate.build_journals(paths, **kwargs)
    path = paths.normalized_event_path(DAY)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["text"] += " More information."
    write_jsonl(path, rows)
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "generated"
    assert len(calls) == 2
    with (paths.state / "journal-build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            generate.build_journals(paths, **kwargs)


@pytest.mark.parametrize("fault", ["error", "length", "wrong_model", "tools", "truncated", "json"])
def test_pi_protocol_rejects_failed_or_wrong_routes(tmp_path: Path, monkeypatch, fault) -> None:
    message: dict[str, Any] = {
        "role": "assistant", "provider": "codex-lb", "model": "gpt-6.1-sol",
        "stopReason": "stop", "content": [{"type": "text", "text": "Prepared.[fn:evt_mail]"}],
    }
    stream: list[dict[str, Any]] = [
        {"type": "message_end", "message": message}, {"type": "agent_settled"}
    ]
    if fault in {"error", "length"}:
        message["stopReason"] = fault
    if fault == "wrong_model":
        message["model"] = "other"
    if fault == "tools":
        stream.insert(0, {"type": "tool_execution_start"})
    if fault == "truncated":
        stream.pop()
    stdout = "\n".join(json.dumps(row) for row in stream)
    if fault == "json":
        stdout = "not JSON"

    def run(args, **kwargs):
        assert "--no-tools" in args and "--no-extensions" in args
        assert "--no-context-files" in args and "--no-session" in args
        assert args[args.index("--provider") + 1] == "codex-lb"
        assert args[args.index("--thinking") + 1] == "medium"
        assert kwargs["input"] == "evidence"
        assert Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(generate.subprocess, "run", run)
    with pytest.raises(ValueError):
        generate.run_pi("evidence", generate.DEFAULT_MODEL)
