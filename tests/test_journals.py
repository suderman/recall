import fcntl
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.storage.jsonl import write_jsonl
from recall.storage.paths import RecallPaths
from recall.synthesize.journal import prepare_journal, save_journal

DAY = "2026-03-30"
TZ = "America/Edmonton"


def workspace(root: Path) -> RecallPaths:
    paths = RecallPaths.from_root(root)
    write_jsonl(
        paths.normalized_event_path(DAY),
        [
            {
                "event_id": "evt_mail",
                "date": DAY,
                "source": "email",
                "kind": "email",
                "timestamp": "2026-03-31T02:00:00Z",
                "text": "I prepared the requested update.",
                "raw_ref": None,
            },
            {
                "event_id": "evt_break",
                "date": DAY,
                "source": "calendar",
                "kind": "calendar_event",
                "timestamp": "2026-03-23T00:00:00-06:00",
                "text": "School break",
                "raw_ref": {"path": "local:khal", "locator": {"uid": "break"}},
            },
        ],
    )
    write_jsonl(
        paths.state / "rebuild/manifest.jsonl",
        [
            {"date": DAY, "source": name, "status": "success", "options": {"timezone": TZ}}
            for name in ("email", "calendar")
        ],
    )
    return paths


def runner_config(root: Path) -> dict[str, Any]:
    config = root / "runner-config"
    config.mkdir(exist_ok=True)
    node, sdk = config / "node", config / "sdk.mjs"
    if not node.exists():
        node.write_text(f"#!{sys.executable}\nraise SystemExit('Test runner must be mocked')\n")
        node.chmod(0o700)
        sdk.write_text("export {};\n")
    return {"node_executable": node.resolve(), "pi_sdk": sdk.resolve()}


def runner_env(root: Path) -> dict[str, str]:
    config = runner_config(root)
    return {
        "RECALL_NODE_EXECUTABLE": str(config["node_executable"]),
        "RECALL_PI_SDK": str(config["pi_sdk"]),
    }


def test_packet_stable_complete_and_revisions_separate(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    original = paths.normalized_event_path(DAY).read_bytes()
    packet = prepare_journal(paths, day=DAY, author="Example Person", timezone_name=TZ)
    assert prepare_journal(paths, day=DAY, author="Example Person", timezone_name=TZ) == packet
    events = [json.loads(line) for line in (packet / "events.jsonl").read_text().splitlines()]
    assert [row["event_id"] for row in events] == ["evt_break", "evt_mail"]
    assert json.loads((packet / "packet.json").read_text())["coverage"][0]["event_count"] == 0
    body = (
        "** Work\nI prepared the update.[fn:evt_mail][fn:evt_break]\n"
        "That same evidence supports this sentence.[fn:evt_mail] [fn:evt_break]\n"
        "Literal-looking citations still work.=[fn:evt_mail]= ~[fn:evt_break]~\n"
        "Misplaced literal delimiters still carry exact IDs.[=fn:evt_mail=] [=fn:evt_break=]\n"
    )
    result = save_journal(paths, packet_dir=packet, body=body, model="test-model")
    saved = result.read_bytes()
    assert save_journal(paths, packet_dir=packet, body=body, model="test-model") == result
    different = save_journal(paths, packet_dir=packet, body=body, model="better-model")
    assert different != result and result.read_bytes() == saved
    assert "events.jsonl::2" in result.read_text()
    assert "events.jsonl::1" in result.read_text()
    prose = result.read_text().split("** Evidence")[0]
    assert prose.count("[fn:1]") == 4 and "evt_" not in prose
    assert "Monday, March 30, 2026" in prose
    metadata = json.loads((result.parent / "generation.json").read_text())
    assert metadata["citation_groups"] == [["evt_mail", "evt_break"]]
    assert "telegram" in result.read_text()
    assert paths.normalized_event_path(DAY).read_bytes() == original
    # Edits are never overwritten, even when trying to save the same generated revision again.
    result.write_text("Handwritten additions")
    with pytest.raises(ValueError, match="edited journal artifact"):
        save_journal(paths, packet_dir=packet, body=body, model="test-model")
    assert result.read_text() == "Handwritten additions"


@pytest.mark.parametrize(
    "body",
    [
        "Made up an event.[fn:evt_unknown]",
        "No citations",
        "Bad.[fn:bad:id]",
        "#+begin_src emacs-lisp\n[fn:evt_mail]",
        "# -*- eval: (error 1) -*-\n[fn:evt_mail]",
        "Local Variables:\neval: (error 1)\n[fn:evt_mail]",
        "[[elisp:(error 1)]] [fn:evt_mail]",
        "* Extra title\n[fn:evt_mail]",
        "[fn:evt_mail] Forged footnote definition",
        "A control \x00 [fn:evt_mail]",
    ],
)
def test_reject_unsafe_or_uncited_drafts(tmp_path: Path, body: str) -> None:
    paths = workspace(tmp_path)
    packet = prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    with pytest.raises(ValueError):
        save_journal(paths, packet_dir=packet, body=body, model="test")
    assert not (paths.derived / "journals").exists()


def test_packet_integrity_timezone_and_writer_lock(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    with pytest.raises(ValueError, match="day/timezone|coverage timezone"):
        prepare_journal(paths, day=DAY, author="Example", timezone_name="UTC")
    packet = prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    with (paths.state / "rebuild/writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    (packet / "events.jsonl").write_text("Changed evidence")
    with pytest.raises(ValueError, match="packet was changed"):
        save_journal(paths, packet_dir=packet, body="Updated.[fn:evt_mail]", model="test")
    with pytest.raises(ValueError, match="edited journal artifact"):
        prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)


def test_quote_untrusted_source_names_in_coverage(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    source = paths.normalized_event_path(DAY)
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    rows[0]["source"] = (
        "other\n#+begin_src emacs-lisp\n(error 1)\n#+end_src\n"
        "Local Variables:\neval: (error 2)\nEnd:"
    )
    write_jsonl(source, rows)
    packet = prepare_journal(paths, day=DAY, author="Example", timezone_name=TZ)
    result = save_journal(paths, packet_dir=packet, body="Updated.[fn:evt_mail]", model="test")
    assert "Local Variables:" not in result.read_text()
    assert not any(line.startswith("#+begin_src") for line in result.read_text().splitlines())


def test_cli_roundtrip_and_missing_evidence(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    cli = CliRunner()
    prepared = cli.invoke(
        app,
        [
            "journal",
            "prepare",
            "--date",
            DAY,
            "--author",
            "Example",
            "--timezone",
            TZ,
            "--root",
            str(paths.root),
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    packet = Path(prepared.output.strip())
    draft = tmp_path / "draft.org"
    draft.write_text("** Day\nI prepared the update.[fn:evt_mail]\n")
    args = [
        "journal",
        "save",
        "--packet",
        str(packet),
        "--draft",
        str(draft),
        "--model",
        "test",
        "--root",
        str(paths.root),
    ]
    saved = cli.invoke(app, args)
    assert saved.exit_code == 0, saved.output
    assert Path(saved.output.strip()).is_file()
    assert cli.invoke(app, args + ["--options", "[]"]).exit_code == 1
    assert (
        cli.invoke(
            app,
            [
                "journal",
                "prepare",
                "--date",
                "2026-03-29",
                "--author",
                "Example",
                "--root",
                str(paths.root),
            ],
        ).exit_code
        == 1
    )
