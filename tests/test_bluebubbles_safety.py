from __future__ import annotations

from urllib.parse import quote, quote_plus

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import recall.connectors.bluebubbles.cli as cli
from recall.cli.main import app
from recall.connectors.bluebubbles.config import BlueBubblesSourceConfig, load_bluebubbles_config
from recall.connectors.bluebubbles.recovery import recover_bluebubbles_messages
from recall.connectors.bluebubbles.webhook import create_bluebubbles_webhook_app
from recall.storage.paths import RecallPaths

SECRET = "FAKE_SECRET +/&"


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("token", [None, "wrong", SECRET])
def test_webhook_requires_configured_token(tmp_path, configured, token):
    paths = RecallPaths.from_root(tmp_path)
    config = BlueBubblesSourceConfig(webhook_token=SECRET if configured else None)
    with TestClient(create_bluebubbles_webhook_app(paths, config)) as client:
        response = client.post(
            "/bluebubbles/webhook",
            params={"token": token} if token else {},
            json={"type": "new-message", "data": {"guid": "fake", "text": "fixture"}},
        )
    authorized = configured and token == SECRET
    assert response.status_code == (200 if authorized else 401)
    assert bool(list(paths.raw.rglob("events.jsonl"))) == authorized
    assert SECRET not in response.text


def test_defaults_are_loopback_and_serve_refuses_without_token(tmp_path, monkeypatch):
    paths = RecallPaths.from_root(tmp_path)
    assert load_bluebubbles_config(paths).webhook_bind_host == "127.0.0.1"
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: calls.append("listener"))
    monkeypatch.setattr(cli, "_run_startup_recovery", lambda *a, **kw: calls.append("recovery"))
    result = CliRunner().invoke(app, ["capture", "bluebubbles", "serve", "--root", str(tmp_path)])
    assert result.exit_code != 0 and "Configure webhook_token" in result.output
    assert calls == []


def test_serve_hides_token_and_disables_query_access_logs(tmp_path, monkeypatch):
    config = BlueBubblesSourceConfig(webhook_bind_host="0.0.0.0", webhook_token=SECRET)
    monkeypatch.setattr(cli, "load_bluebubbles_config", lambda *a: config)
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: calls.append(kw))
    result = CliRunner().invoke(
        app, ["capture", "bluebubbles", "serve", "--skip-recovery", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "?token=<configured-token>" in result.output
    assert not any(value in result.output for value in (SECRET, quote(SECRET), quote_plus(SECRET)))
    assert calls == [{"host": "0.0.0.0", "port": 8042, "access_log": False}]


@pytest.mark.parametrize("failure", ["http", "exception"])
def test_recovery_errors_hide_password(tmp_path, failure):
    class Client:
        def close(self):
            pass

        def post(self, url, **kwargs):
            if failure == "exception":
                raise RuntimeError(f"failed {url} password={SECRET}")
            return httpx.Response(403, request=httpx.Request("POST", url))

    with pytest.raises(RuntimeError) as error:
        recover_bluebubbles_messages(
            RecallPaths.from_root(tmp_path),
            account="personal",
            server_url="https://fake.invalid",
            password=SECRET,
            client=Client(),
        )
    message = str(error.value)
    assert not any(value in message for value in (SECRET, quote(SECRET), quote_plus(SECRET)))
    assert "403" in message if failure == "http" else "RuntimeError" in message


def test_startup_recovery_errors_are_safe_even_for_custom_clients(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError(f"custom failure includes {SECRET}")

    monkeypatch.setattr(cli, "recover_bluebubbles_messages", failed)
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", SECRET)
    config = BlueBubblesSourceConfig(server_url="https://fake.invalid", webhook_token=SECRET)
    monkeypatch.setattr(cli, "load_bluebubbles_config", lambda *a: config)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: None)
    result = CliRunner().invoke(app, ["capture", "bluebubbles", "serve", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "recovery_status=failed" in result.output
    assert SECRET not in result.output
