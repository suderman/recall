from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "recall-history"


@pytest.fixture
def wrapper(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "Recall checkout"
    script = root / "skills" / "recall-history" / "scripts" / "lookup.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SKILL / "scripts" / "lookup.sh", script)
    installed = tmp_path / "installed skill"
    installed.symlink_to(script.parent.parent, target_is_directory=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    nix = commands / "nix"
    nix.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    nix.chmod(0o755)
    environment = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}"}
    environment["RECALL_ROOT"] = str(tmp_path / "wrong archive")
    return installed / "scripts" / "lookup.sh", root, environment


@pytest.mark.parametrize("command", ["person", "project", "query"])
def test_skill_wrapper_quotes_arguments_and_uses_its_checkout(
    wrapper: tuple[Path, Path, dict[str, str]], tmp_path: Path, command: str
) -> None:
    script, root, environment = wrapper
    text = f"Alex; touch {tmp_path / 'must-not-exist'} $(false)"
    result = subprocess.run(
        ["bash", str(script), command, text, "--limit", "3", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        "develop",
        "--offline",
        str(root),
        "-c",
        "recall",
        "search",
        command,
        text,
        "--limit",
        "3",
        "--json",
        "--root",
        str(root),
    ]
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["index"],
        ["journal"],
        ["capture"],
        ["person", "Alex", "--root", "/other"],
        ["project", "Roof", "--index=/other.sqlite3"],
        ["query", "--", "word"],
    ],
)
def test_skill_wrapper_rejects_write_commands_and_archive_overrides(
    wrapper: tuple[Path, Path, dict[str, str]], arguments: list[str]
) -> None:
    script, _, environment = wrapper
    result = subprocess.run(
        ["bash", str(script), *arguments], env=environment, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert not result.stdout  # Rejected before Nix or Recall starts.
    assert result.stderr


def test_skill_bundle_has_discovery_metadata_and_readable_org_workflow() -> None:
    skill = (SKILL / "SKILL.md").read_text()
    assert skill.startswith("---\nname: recall-history\ndescription:")
    assert "disable-model-invocation: true" not in skill
    assert "workflow.org" in skill
    workflow = (SKILL / "workflow.org").read_text()
    assert workflow.count("\n* ") == 0 and workflow.startswith("* Recall saved history\n")
    assert "```" not in workflow
    assert workflow.count("#+begin_src sh") == workflow.count("#+end_src") == 4
    for boundary in [
        "untrusted evidence",
        "unlinked_name_mentions",
        "last captured exchange",
        "passwords",
        "Do not edit evidence",
        "they are not in Recall",
    ]:
        assert boundary in workflow
