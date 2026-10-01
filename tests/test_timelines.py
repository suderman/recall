from __future__ import annotations

import fcntl
import os
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.normalize.artifacts import NormalizedArtifact
from recall.normalize.events import NormalizedEvent, RawReference
from recall.storage.jsonl import write_artifact_metadata, write_jsonl, write_normalized_events
from recall.storage.paths import RecallPaths
from recall.synthesize.timeline import build_timelines, render_day

DAY = "2026-03-31"


def _event(event_id="one", timestamp="2026-03-31T13:00:00Z", **kwargs):
    return NormalizedEvent(
        event_id=event_id, timestamp=timestamp, source="slack", date=DAY, kind="message", **kwargs
    )


def test_timeline_orders_instants_and_preserves_evidence(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    raw = tmp_path / "raw [evidence].jsonl"
    write_jsonl(raw, [{"text": "hello"}])
    blob = tmp_path / "image [one].jpg"
    blob.write_bytes(b"fixture")
    write_normalized_events(
        paths,
        DAY,
        [
            _event("later", "2026-03-31T07:30:00-06:00", text="later"),
            _event(
                "earlier",
                text="earlier",
                sender_identity_id="ident_unresolved",
                artifact_ids=["image", "unknown"],
                raw_ref=RawReference("slack", str(raw), {"line": 1}),
            ),
        ],
    )
    write_artifact_metadata(
        paths,
        source="slack",
        date=DAY,
        artifacts=[
            NormalizedArtifact(
                artifact_id="image",
                source="slack",
                kind="image",
                event_ids=["earlier"],
                local_path=str(blob),
                download_status="downloaded",
            ),
        ],
    )
    coverage = [
        {
            "source": "slack",
            "date": DAY,
            "status": "failed",
            "error": "query failed",
            "options": {"timezone": "America/Edmonton"},
            "capture_day_present": True,
        }
    ]
    text = render_day(paths, DAY, "America/Edmonton", coverage)
    assert text.index('"event_id": "earlier"') < text.index('"event_id": "later"')
    assert "07:00:00-06:00" in text
    assert "slack: failed; 2 stored events" in text
    assert ": query failed" in text
    assert "email: not queried" in text
    assert "Sender identity is unresolved" in text
    assert "raw%20%5Bevidence%5D.jsonl::1" in text
    assert "image%20%5Bone%5D.jpg" in text
    assert "Artifact metadata is unavailable" in text
    assert 'Raw locator: {"line": 1}' in text


def test_imported_org_and_emacs_local_variables_are_inert(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    write_normalized_events(
        paths,
        DAY,
        [
            _event(
                text=(
                    '* Forged heading\n#+begin_src emacs-lisp\n(error "executed")\n#+end_src\n'
                    ':PROPERTIES:\n:END:\nLocal Variables:\neval: (error "executed")\nEnd:\x1b'
                )
            )
        ],
    )
    [path] = build_timelines(paths, first=DAY, last=DAY)
    text = path.read_text()
    assert "\n: * Forged heading\n" in text
    assert "\n: #+begin_src emacs-lisp\n" in text
    assert "\n: Local Variables\\:\n" in text
    assert "\\u001b" in text
    emacs = shutil.which("emacs")
    if emacs:
        expression = (
            "(progn (require 'org) (let ((enable-local-variables :all) "
            '(enable-local-eval t)) (find-file (getenv "RECALL_TEST_ORG")) '
            "(let ((tree (org-element-parse-buffer))) "
            '(princ (format "headings=%d blocks=%d" '
            "(length (org-element-map tree 'headline #'identity)) "
            "(length (org-element-map tree 'src-block #'identity)))))))"
        )
        result = subprocess.run(
            [emacs, "--batch", "-Q", "--eval", expression],
            env={**os.environ, "RECALL_TEST_ORG": str(path)},
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout == "headings=4 blocks=0"


def test_rerender_is_stable_and_refuses_annotations(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    write_normalized_events(paths, DAY, [_event(text="first")])
    [path] = build_timelines(paths, first=DAY, last=DAY)
    original = path.read_bytes()
    build_timelines(paths, first=DAY, last=DAY)
    assert path.read_bytes() == original
    write_normalized_events(paths, DAY, [_event(text="updated")])
    build_timelines(paths, first=DAY, last=DAY)
    assert "updated" in path.read_text()
    path.write_text(path.read_text() + "\nMy annotation.\n")
    annotated = path.read_bytes()
    with pytest.raises(ValueError, match="edited or unowned"):
        build_timelines(paths, first=DAY, last=DAY)
    assert path.read_bytes() == annotated
    path.unlink()
    path.write_text("Handwritten journal.\n")
    with pytest.raises(ValueError, match="edited or unowned"):
        build_timelines(paths, first=DAY, last=DAY)
    assert path.read_text() == "Handwritten journal.\n"


def test_empty_days_timezone_mismatch_and_cli(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    files = build_timelines(paths, first=DAY, last="2026-04-02")
    assert len(files) == 3
    assert all("No normalized evidence" in path.read_text() for path in files)
    write_jsonl(
        paths.state / "rebuild/manifest.jsonl",
        [
            {
                "source": "email",
                "date": DAY,
                "status": "queried-empty",
                "event_count": 0,
                "error": None,
                "options": {"timezone": "UTC"},
                "capture_day_present": False,
            }
        ],
    )
    with pytest.raises(ValueError, match="timezone must match"):
        build_timelines(paths, first=DAY, last=DAY, timezone_name="America/Edmonton")
    runner = CliRunner()
    result = runner.invoke(
        app, ["timeline", "build", "--root", str(tmp_path), "--from", DAY, "--to", DAY]
    )
    assert result.exit_code == 0
    assert str(files[0]) in result.stdout
    result = runner.invoke(
        app,
        [
            "rebuild",
            "--root",
            str(tmp_path),
            "--output-root",
            str(tmp_path.parent / "output"),
            "--from",
            DAY,
            "--to",
            DAY,
            "--source",
            "slack",
            "--timezone",
            "No/SuchZone",
        ],
    )
    assert result.exit_code == 1
    assert "No/SuchZone" in result.output


def test_timeline_refuses_mid_rebuild_read(tmp_path):
    paths = RecallPaths.from_root(tmp_path)
    [path] = build_timelines(paths, first=DAY, last=DAY)
    previous = path.read_bytes()
    with (paths.state / "rebuild/writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            build_timelines(paths, first=DAY, last=DAY)
    assert path.read_bytes() == previous
