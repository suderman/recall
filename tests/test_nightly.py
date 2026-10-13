import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("failed_stage", "expected_status", "expected_last_command"),
    [("", 0, "journal"), ("capture", 1, "journal"), ("rebuild", 2, "rebuild")],
)
def test_nightly_reports_capture_failure_after_publication(
    tmp_path, failed_stage, expected_status, expected_last_command
):
    home = tmp_path / "recall"
    config = home / "archive/config/sources"
    config.mkdir(parents=True)
    (config / "slack.toml").write_text("")
    commands = tmp_path / "commands"
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "recall"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$1" >> "$COMMAND_LOG"\n'
        'if [[ $1 == "$FAILED_STAGE" ]]; then exit 2; fi\n'
    )
    stub.chmod(0o700)
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "examples/nightly.sh"), "2026-10-12"],
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RECALL_HOME": str(home),
            "RECALL_AUTHOR": "Jon Suderman",
            "RECALL_JOURNAL_OUTPUT": str(tmp_path / "journal"),
            "COMMAND_LOG": str(commands),
            "FAILED_STAGE": failed_stage,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_status, result.stderr
    assert commands.read_text().splitlines()[-1] == expected_last_command
    assert ("Slack capture failed" in result.stderr) == (failed_stage == "capture")
