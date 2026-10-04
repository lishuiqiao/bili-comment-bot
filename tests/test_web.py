"""Offline console API, persistence, lifecycle and secret-boundary regression tests."""

import asyncio
import io
import json
from email.message import Message
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bili_comment_bot.config import Settings, load_settings, web_config_path
from bili_comment_bot.web import Console, ConsoleError, handler_for, public_settings


@pytest.fixture
def console(tmp_path, monkeypatch):
    for key in tuple(__import__("os").environ):
        if key.startswith("BILI_BOT_"):
            monkeypatch.delenv(key)
    value = Console(tmp_path / "config.toml")
    value.settings = Settings(data_dir=tmp_path / "data")
    return value


def payload(console):
    return {"settings": public_settings(console.settings), "revision": console.revision}


async def test_save_reload_secret_preservation_clear_and_override(console, monkeypatch):
    raw = payload(console)
    raw["settings"]["ai"].update(model="synthetic-model", api_key="synthetic-secret")
    raw["settings"]["transcription"]["api_key"] = "synthetic-transcription"
    raw["settings"]["persona"]["name"] = "测试机器人"
    result = await console.action("/api/config", raw)
    assert "synthetic-secret" not in json.dumps(result)
    assert "synthetic-transcription" not in json.dumps(result)
    assert result["secrets"] == {"ai": True, "transcription": True}
    assert web_config_path(console.path).stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("BILI_BOT_AI_MODEL", "legacy-environment")
    loaded = load_settings(console.path)
    assert loaded.ai.model == "synthetic-model"
    assert loaded.persona.name == "测试机器人"
    assert loaded.ai.api_key.get_secret_value() == "synthetic-secret"
    await console.action("/api/config", payload(console))
    assert console.settings.ai.api_key.get_secret_value() == "synthetic-secret"
    raw = payload(console)
    raw["settings"]["ai"]["api_key"] = None
    await console.action("/api/config", raw)
    assert not load_settings(console.path).ai.api_key.get_secret_value()
    assert load_settings(console.path).transcription.api_key.get_secret_value()


async def test_invalid_or_stale_save_does_not_stop_or_write(console):
    console._stop = AsyncMock()
    raw = payload(console)
    raw["settings"]["limits"]["dm_per_hour"] = -1
    with pytest.raises(ValidationError):
        await console.action("/api/config", raw)
    raw = payload(console)
    raw["revision"] = 42
    with pytest.raises(ConsoleError):
        await console.action("/api/config", raw)
    console._stop.assert_not_awaited()
    assert not web_config_path(console.path).exists()


async def test_running_save_stops_old_namespace_before_starting_new(console):
    console.state = "running"
    calls = []

    async def stop():
        calls.append(("stop", console.settings.namespace))
        console.state = "stopped"

    async def start():
        assert load_settings(console.path).namespace == "live"
        calls.append(("start", console.settings.namespace))

    console._stop, console._start = stop, start
    raw = payload(console)
    raw["settings"]["publishing"] = {"dry_run": False, "publish_enabled": True}
    await console.action("/api/config", raw)
    assert calls == [("stop", "sim"), ("start", "live")]


async def test_login_blocks_saving_and_login_stops_worker(console):
    console.state = "login"
    with pytest.raises(ConsoleError):
        await console.action("/api/config", payload(console))
    calls = []

    async def stop():
        calls.append("stop")

    async def start(command):
        calls.append(command)

    console._stop, console._start = stop, start
    await console.action("/api/login", {})
    assert calls == ["stop", "login"]


async def test_save_failure_keeps_old_configuration(console, monkeypatch):
    console.state = "running"
    console._start = AsyncMock()

    def fail(*_):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("bili_comment_bot.web.write_private", fail)
    raw = payload(console)
    raw["settings"]["persona"]["name"] = "New"
    with pytest.raises(OSError):
        await console.action("/api/config", raw)
    assert console.settings.persona.name == "B站评论机器人"
    assert console.state == "stopped"
    console._start.assert_not_awaited()


async def request(console, path="/api/state", body=None, *, auth=True, host=None, origin=None):
    cls = handler_for(console, asyncio.get_running_loop(), {"127.0.0.1:8765"})
    handler = object.__new__(cls)
    handler.path = path
    handler.headers = Message()
    handler.headers["Host"] = host or "127.0.0.1:8765"
    if auth:
        handler.headers["Authorization"] = "Bearer " + console.token
    if origin:
        handler.headers["Origin"] = origin
    result = []
    handler.reply = lambda *values: result.append(values)
    if body is not None:
        data = json.dumps(body).encode()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(data))
        handler.rfile = io.BytesIO(data)
        handler.connection = type("Connection", (), {"settimeout": lambda *_: None})()
    await asyncio.to_thread(handler.do_GET if body is None else handler.do_POST)
    return result[0]


async def test_http_auth_origin_host_and_secret_safe_validation(console):
    assert (await request(console, auth=False))[0] == 401
    assert (await request(console, host="attacker.example:8765"))[0] == 403
    assert (await request(console, origin="https://attacker.example"))[0] == 403
    raw = payload(console)
    raw["settings"]["ai"]["api_key"] = {"synthetic-secret": "must-not-leak"}
    code, body = await request(console, "/api/config", raw)
    assert code == 422
    assert body["fields"][0]["path"] == "ai.api_key"
    assert "synthetic-secret" not in json.dumps(body)
    assert "must-not-leak" not in json.dumps(body)
    code, body = await request(console)
    assert code == 200 and body["settings"]["ai"]["api_key"] == ""


async def test_qr_requires_auth_and_active_fresh_login(console):
    console.settings.data_dir.mkdir()
    image = console.settings.data_dir / "login.png"
    image.write_bytes(b"synthetic PNG")
    assert (await request(console, "/api/qr", auth=False))[0] == 401
    assert (await request(console, "/api/qr"))[0] == 404
    console.state = "login"
    console.login_started = image.stat().st_mtime + 1
    assert (await request(console, "/api/qr"))[0] == 404
    console.login_started = 0
    assert (await request(console, "/api/qr"))[:2] == (200, b"synthetic PNG")


async def test_static_allowlist_and_no_file_traversal(console):
    assert (await request(console, "/", auth=False))[0] == 200
    assert (await request(console, "/app.js", auth=False))[0] == 200
    assert (await request(console, "/style.css", auth=False))[0] == 200
    assert (await request(console, "/../config.toml", auth=False))[0] == 404


def test_bootstrap_legacy_and_broken_config_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("BILI_BOT_WEB_CONFIG", raising=False)
    path = tmp_path / "config.toml"
    assert load_settings(path).publishing.dry_run
    path.write_text('[persona]\nname="Legacy"\n')
    assert load_settings(path).persona.name == "Legacy"
    path.write_text("invalid TOML !!!")
    console = Console(path)
    assert "无法读取" in console.message
    assert console.state == "stopped"


async def test_cli_default_opens_console(monkeypatch, tmp_path):
    import sys

    from bili_comment_bot import __main__ as cli

    serve = AsyncMock()
    monkeypatch.setattr("bili_comment_bot.web.serve", serve)
    monkeypatch.setattr(sys, "argv", ["bili-comment-bot", "--config", str(tmp_path / "new.toml")])
    await asyncio.to_thread(cli.main)
    serve.assert_awaited_once()


async def test_stop_waits_for_process_and_watcher(console):
    class Process:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            await asyncio.sleep(0)
            return self.returncode

    console.process = Process()
    console.monitor = asyncio.create_task(asyncio.sleep(0))
    await console._stop()
    assert console.process is None
    assert console.monitor.done()
    assert console.state == "stopped"


@pytest.mark.parametrize("option", ["--once", "--headless"])
async def test_cli_legacy_worker_commands_skip_console(monkeypatch, tmp_path, option):
    import sys

    from bili_comment_bot import __main__ as cli

    worker, serve = AsyncMock(), AsyncMock()
    monkeypatch.setattr("bili_comment_bot.runtime.run_bot", worker)
    monkeypatch.setattr("bili_comment_bot.web.serve", serve)
    monkeypatch.setattr(
        sys, "argv", ["bili-comment-bot", "--config", str(tmp_path / "new.toml"), "run", option]
    )
    await asyncio.to_thread(cli.main)
    worker.assert_awaited_once()
    assert worker.call_args.kwargs["once"] == (option == "--once")
    serve.assert_not_awaited()


async def test_shutdown_rejects_queued_mutations(console):
    console.closing = True
    console._start = AsyncMock()
    with pytest.raises(ConsoleError):
        await console.action("/api/start", {})
    console._start.assert_not_awaited()


def test_status_exposes_only_boolean_health(console, monkeypatch):
    monkeypatch.setattr(
        "bili_comment_bot.web.read_status",
        lambda *_: {
            "healthy": True,
            "private_message": "synthetic-private-message",
            "cookies": "synthetic-private-cookie",
            "local_path": "synthetic-private-path",
            "account_uid": 12345,
        },
    )
    response = console.snapshot()
    assert response["status"] == {"healthy": True}
    assert "synthetic-private" not in json.dumps(response)


async def test_site_uses_public_chinese_name(console):
    code, body, _ = await request(console, "/", auth=False)
    assert code == 200
    assert "B站评论机器人" in body.decode()
    assert "B站评论机器人" == console.settings.persona.name
