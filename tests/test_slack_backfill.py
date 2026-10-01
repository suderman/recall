from __future__ import annotations

import fcntl
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from recall.cli.main import app
from recall.connectors.slack import backfill as module
from recall.connectors.slack.api import SlackApiClient
from recall.connectors.slack.backfill import backfill_slack
from recall.connectors.slack.capture import read_json, read_jsonl
from recall.normalize.rebuild import rebuild_range
from recall.storage.paths import RecallPaths


class History:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at
        self.oldest = "0"
        self.workspace = "T123"

    def auth_test(self) -> dict[str, Any]:
        return {"team_id": self.workspace, "user_id": "USELF"}

    def list_users(self) -> list[dict[str, Any]]:
        return [{"id": "USELF", "name": "Example"}]

    def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        return [{"id": "C123", "name": "example"}]

    def fetch_history(
        self, channel_id: str, *, oldest: str, latest: str, inclusive: bool = True
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("fixture source unavailable")
        assert inclusive
        self.oldest = oldest
        end = {"ts": latest, "user": "USELF", "text": "Last microsecond"}
        return [
            {"ts": oldest, "user": "USELF", "files": [{"id": "F123"}], "reply_count": 1},
            {"ts": str(Decimal(oldest) + 1), "text": "<!date^1|A scheduled detail>"},
            end,
            dict(end),
            {"ts": str(Decimal(latest) + Decimal("0.000001")), "text": "Next day"},
        ]

    def fetch_replies(self, channel_id: str, *, ts: str) -> list[dict[str, Any]]:
        return [
            {"ts": str(Decimal(ts) + 2), "text": "An in-window reply"},
            {"ts": str(Decimal(ts) - 1), "text": "Outside day"},
        ]


def run(tmp_path: Path, client: History, **options: Any) -> list[dict[str, Any]]:
    return backfill_slack(
        RecallPaths.from_root(tmp_path / "source"),
        RecallPaths.from_root(tmp_path / "output"),
        client=client,
        first=options.pop("first", "2026-03-08"),
        last=options.pop("last", "2026-03-08"),
        timezone_name=options.pop("timezone_name", "America/Edmonton"),
        account="work",
        **options,
    )


def fingerprint(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "day,duration", [("2026-03-08", "82799.999999"), ("2026-11-01", "89999.999999")]
)
def test_backfill_preserves_raw_message_shapes_exact_bounds_and_cached_bytes(
    tmp_path: Path,
    day: str,
    duration: str,
) -> None:
    client = History()
    result = run(tmp_path, client, first=day, last=day)
    assert result[0]["status"] == "captured" and result[0]["stored_messages"] == 4
    paths = RecallPaths.from_root(tmp_path / "output")
    raw = paths.raw_capture_dir("slack", day)
    metadata = read_json(raw / "metadata.json")
    window = metadata["backfill_window"]
    assert Decimal(window["latest"]) - Decimal(window["oldest"]) == Decimal(duration)
    messages = read_jsonl(raw / "messages.jsonl")
    assert any(
        item["message"].get("files") and not item["message"].get("text") for item in messages
    )
    assert any("<!date^" in item["message"].get("text", "") for item in messages)
    assert len({item["message"]["ts"] for item in messages}) == 4
    before = fingerprint(paths.raw)
    assert run(tmp_path, client, first=day, last=day)[0]["reused"] and client.calls == 1
    assert fingerprint(paths.raw) == before
    assert result[0]["coverage_limits"]
    assert not paths.database.exists() and not paths.cursors.exists()
    assert not (tmp_path / "source").exists()


def test_backfill_failure_stops_then_resumes_without_rewriting_success(tmp_path: Path) -> None:
    first = run(tmp_path, History(fail_at=2), last="2026-03-10")
    assert [job["status"] for job in first] == ["captured", "failed"]
    output = RecallPaths.from_root(tmp_path / "output")
    old = fingerprint(output.raw_capture_dir("slack", "2026-03-08"))
    assert not output.raw_capture_dir("slack", "2026-03-09").exists()
    assert not output.raw_capture_dir("slack", "2026-03-10").exists()
    source = History()
    resumed = run(tmp_path, source, last="2026-03-10")
    assert len(resumed) == 3 and resumed[0]["reused"] and source.calls == 2
    assert all(job["status"] == "captured" for job in resumed)
    assert fingerprint(output.raw_capture_dir("slack", "2026-03-08")) == old
    other = History()
    other.workspace = "DIFFERENT"
    rejected = run(tmp_path, other, first="2026-03-11", last="2026-03-11")
    assert rejected[0]["status"] == "failed"
    assert not output.raw_capture_dir("slack", "2026-03-11").exists()


def test_backfill_publication_failure_and_lost_state_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rename = module.os.rename
    with monkeypatch.context() as patch:
        patch.setattr(
            module.os,
            "rename",
            lambda *args: (_ for _ in ()).throw(OSError("fixture disk failure")),
        )
        assert run(tmp_path, History())[0]["status"] == "failed"
    output = RecallPaths.from_root(tmp_path / "output")
    assert not output.raw_capture_dir("slack", "2026-03-08").exists()
    assert not list((output.raw / "slack").glob(".slack-backfill-*"))
    assert module.os.rename is rename
    assert run(tmp_path, History())[0]["status"] == "captured"
    (output.state / "backfill" / "slack.jsonl").unlink()
    wrong_workspace = History()
    wrong_workspace.workspace = "DIFFERENT"
    rejected = run(tmp_path, wrong_workspace, first="2026-03-09", last="2026-03-09")
    assert rejected[0]["status"] == "failed"
    assert not output.raw_capture_dir("slack", "2026-03-09").exists()
    source = History()
    assert run(tmp_path, source)[0]["reused"] and source.calls == 0


def test_backfill_refuses_existing_changed_conflicting_and_overlapping_outputs(
    tmp_path: Path,
) -> None:
    run(tmp_path, History())
    output = RecallPaths.from_root(tmp_path / "output")
    saved = fingerprint(output.raw)
    with pytest.raises(ValueError, match="incompatible"):
        run(tmp_path, History(), timezone_name="UTC")
    assert fingerprint(output.raw) == saved
    messages_path = output.raw_capture_dir("slack", "2026-03-08") / "messages.jsonl"
    original = messages_path.read_bytes()
    messages_path.write_text("changed\n")
    with pytest.raises(ValueError, match="changed"):
        run(tmp_path, History())
    messages_path.write_bytes(original)
    unowned = output.raw_capture_dir("slack", "2026-04-01")
    unowned.mkdir()
    (unowned / "messages.jsonl").write_text("original")
    with pytest.raises(ValueError, match="Refusing"):
        run(tmp_path, History(), first="2026-04-01", last="2026-04-01")
    assert (unowned / "messages.jsonl").read_text() == "original"
    for root in [output.root, output.root / "nested", output.root.parent]:
        with pytest.raises(ValueError, match="overlap"):
            backfill_slack(
                output,
                RecallPaths.from_root(root),
                client=History(),
                first="2026-03-08",
                last="2026-03-08",
                account="work",
                timezone_name="UTC",
            )
    with pytest.raises(ValueError):
        run(tmp_path, History(), first="2026-03-09", last="2026-03-08")
    with (output.state / "backfill" / "slack.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Another"):
            run(tmp_path, History())


def test_empty_capture_is_queried_empty_not_complete_life_coverage(tmp_path: Path) -> None:
    class Empty(History):
        def list_conversations(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
            return []

    row = run(tmp_path, Empty())[0]
    assert row["status"] == "queried-empty" and row["stored_messages"] == 0
    assert row["coverage_limits"]


def test_cli_requires_token_and_reports_failed_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import recall.cli.backfill as cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    args = [
        "backfill",
        "slack",
        "--from",
        "2026-03-08",
        "--to",
        "2026-03-09",
        "--root",
        str(tmp_path / "source"),
        "--output-root",
        str(tmp_path / "output"),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and "Missing Slack token" in result.output
    assert not (tmp_path / "output").exists()
    monkeypatch.setenv("SLACK_USER_TOKEN", "fixture-token")

    class Client(History):
        def __init__(self, token: str) -> None:
            assert token == "fixture-token"
            super().__init__(fail_at=2)

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(cli, "SlackApiClient", Client)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1 and '"status": "failed"' in result.output
    assert "fixture-token" not in result.output


def test_slack_api_cursor_pages_and_rate_limit_retry_budget() -> None:
    calls = []
    sleeps = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if not request.url.params.get("cursor"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"ts": "1"}],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "next"},
                },
            )
        return httpx.Response(200, json={"ok": True, "messages": [{"ts": "2"}]})

    with httpx.Client(
        base_url="https://slack.com/api/", transport=httpx.MockTransport(handle)
    ) as http:
        api = SlackApiClient("fixture", client=http, sleep_fn=sleeps.append)
        assert len(api.fetch_history("C123", oldest="0", latest="3")) == 2
    assert sleeps == [2] and calls[-1]["cursor"] == "next"
    with httpx.Client(
        base_url="https://slack.com/api/",
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "1"})),
    ) as http:
        sleeps.clear()
        with pytest.raises(RuntimeError, match="budget"):
            SlackApiClient(
                "fixture", client=http, sleep_fn=sleeps.append, max_rate_limit_retries=2
            ).auth_test()
        assert sleeps == [1, 1]


@pytest.mark.parametrize(
    "payload", [{"ok": True, "is_limited": True}, {"ok": True, "has_more": True}]
)
def test_slack_api_refuses_silent_partial_pages(payload: dict) -> None:
    with httpx.Client(
        base_url="https://slack.com/api/",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as http:
        with pytest.raises(RuntimeError):
            SlackApiClient("fixture", client=http).fetch_history("C123", oldest="0", latest="3")


def test_backfill_refuses_output_symlinks_into_source(tmp_path: Path) -> None:
    source = RecallPaths.from_root(tmp_path / "source")
    output = RecallPaths.from_root(tmp_path / "output")
    source.raw.mkdir(parents=True)
    (source.raw / "original").write_text("evidence")
    output.data.mkdir(parents=True)
    output.raw.symlink_to(source.raw, target_is_directory=True)
    before = fingerprint(source.root)
    with pytest.raises(ValueError, match="inside"):
        run(tmp_path, History())
    assert fingerprint(source.root) == before


def test_interrupted_backfill_keeps_live_state_and_resumes_prior_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RecallPaths.from_root(tmp_path / "source")
    source.state.mkdir(parents=True)
    source.database.write_bytes(b"Existing live cursor and identity storage")
    before = fingerprint(source.root)
    client = History()
    fetch = client.fetch_history

    def interrupted(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        rows = fetch(*args, **kwargs)
        if client.calls == 2:
            raise KeyboardInterrupt
        return rows

    monkeypatch.setattr(client, "fetch_history", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, client, last="2026-03-10")
    resumed = run(tmp_path, History(), last="2026-03-10")
    assert resumed[0]["reused"] and all(row["status"] == "captured" for row in resumed)
    assert fingerprint(source.root) == before


def test_backfill_bundles_replay_without_new_event_ids_or_raw_writes(tmp_path: Path) -> None:
    identities = []
    for name in ["first", "second"]:
        workspace = tmp_path / name
        run(workspace, History())
        inputs = RecallPaths.from_root(workspace / "output")
        output = RecallPaths.from_root(workspace / "normalized")
        before = fingerprint(inputs.raw)
        jobs = rebuild_range(
            inputs,
            output,
            first="2026-03-08",
            last="2026-03-08",
            sources=["slack"],
            timezone_name="America/Edmonton",
            account="work",
        )
        assert jobs[0]["status"] == "success"
        rows = read_jsonl(output.normalized_event_path("2026-03-08"))
        assert len(rows) == 4 and all(row["date"] == "2026-03-08" for row in rows)
        identities.append({row["event_id"] for row in rows})
        assert len(identities[-1]) == 4
        assert all(Path(row["raw_ref"]["path"]).exists() for row in rows)
        assert fingerprint(inputs.raw) == before
    assert identities[0] == identities[1]


def test_slack_raw_parse_errors_keep_file_and_line_context(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{broken}\n")
    with pytest.raises(ValueError, match="broken.json:1"):
        read_json(path)
    path.write_text("{}\n{broken}\n")
    with pytest.raises(ValueError, match="broken.json:2"):
        read_jsonl(path)
