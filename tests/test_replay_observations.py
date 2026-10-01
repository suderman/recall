from __future__ import annotations

from pathlib import Path

import pytest

import recall.connectors.email.notmuch as email
from recall.lookup import person
from recall.normalize.rebuild import rebuild_range
from recall.search import build_index
from recall.storage.jsonl import read_jsonl, write_jsonl
from recall.storage.paths import RecallPaths


def test_replay_retains_header_names_without_writing_primary_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = RecallPaths.from_root(tmp_path / "inputs")
    output = RecallPaths.from_root(tmp_path / "output")
    fixture = Path(__file__).parent / "fixtures/email/messages/conversation.eml"
    monkeypatch.setattr(email, "run_notmuch_command", lambda _: str(fixture))
    options = {"first": "2026-03-31", "last": "2026-03-31", "sources": ["email"]}
    jobs = rebuild_range(inputs, output, **options)
    assert jobs[0]["status"] == "success"
    snapshot = output.state / "rebuild/identity-labels.jsonl"
    assert snapshot.is_file()
    labels = {row["identity_id"]: row["labels"] for row in read_jsonl(snapshot)}
    assert "Ariel Example" in labels["ident_email_ariel_example_com"]
    index = Path(build_index([output])["index"])
    packet = person(index, "Ariel Example")
    assert len(packet["candidates"]) == 1
    assert packet["candidates"][0]["last_captured"]["event"]["source"] == "email"
    assert not inputs.root.exists()
    assert not output.database.exists()  # Retain observations, not mutable resolution state.
    before = snapshot.read_bytes()
    rebuild_range(inputs, output, **options)
    assert snapshot.read_bytes() == before

    def fail(_: list[str]) -> str:
        raise RuntimeError("Unavailable query")

    monkeypatch.setattr(email, "run_notmuch_command", fail)
    assert rebuild_range(inputs, output, **options)[0]["status"] == "failed"
    assert snapshot.read_bytes() == before
    previous_index = index.read_bytes()
    write_jsonl(
        snapshot, [{"identity_id": "ident_email_ariel_example_com", "labels": "not a list"}]
    )
    with pytest.raises(ValueError, match="Invalid identity observations"):
        build_index([output])
    assert index.read_bytes() == previous_index
