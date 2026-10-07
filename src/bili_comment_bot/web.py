"""Local configuration console with authenticated API and isolated worker lifecycle."""

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import ValidationError

from .config import Settings, load_settings, web_config_path
from .instance_lock import InstanceLock
from .observability import read_status, write_private

ASSETS = Path(__file__).with_name("web_assets")


def private_settings(settings):
    value = settings.model_dump(mode="json")
    for section in ("ai", "transcription"):
        value[section]["api_key"] = getattr(settings, section).api_key.get_secret_value()
    return value


def public_settings(settings):
    value = settings.model_dump(mode="json")
    for section in ("ai", "transcription"):
        value[section]["api_key"] = ""
    return value


class ConsoleError(ValueError):
    """Safe, user-facing messages without supplied configuration values."""


class Console:
    def __init__(self, path):
        self.path = path.resolve()
        try:
            self.settings = load_settings(self.path)
            load_error = False
        except (OSError, ValueError):
            self.settings = Settings(data_dir=Path(os.environ.get("BILI_BOT_DATA_DIR", "data")))
            load_error = True
        self.process = None
        self.monitor = None
        self.state = "stopped"
        self.message = "配置模型并扫码登录，然后启动机器人。"
        if load_error:
            self.message = "原配置无法读取，当前显示默认值；请在网页重新配置并保存。"
        self.login_started = 0
        self.lock = asyncio.Lock()
        self.closing = False
        self.revision = 0
        self.token = secrets.token_urlsafe(32)

    def snapshot(self):
        status = None
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            snapshot = read_status(self.settings.data_dir, self.settings.namespace)
            # The UI only needs a health indicator, never arbitrary persisted data.
            status = {"healthy": snapshot.get("healthy") is True}
        return {
            "settings": public_settings(self.settings),
            "schema": Settings.model_json_schema(),
            "secrets": {
                name: bool(getattr(self.settings, name).api_key.get_secret_value())
                for name in ("ai", "transcription")
            },
            "revision": self.revision,
            "state": self.state,
            "message": self.message,
            "mode": self.settings.namespace,
            "status": status,
        }

    async def _watch(self, process, command):
        # CLI output is already sanitized, but expose only fixed progress phrases.
        async for line in process.stdout:
            text = line.decode("utf-8", errors="replace")
            if command == "login":
                for phrase in ("等待扫码", "等待手机确认", "扫码成功", "登录完成", "二维码已过期"):
                    if phrase in text:
                        self.message = phrase
        code = await process.wait()
        if self.process is process:
            self.state = "stopped" if code == 0 else "error"
            self.message = (
                "登录成功，可以启动机器人。"
                if command == "login" and code == 0
                else "机器人已停止。"
                if code == 0
                else "操作失败，请检查模型配置、账号登录状态及服务日志。"
            )

    async def _stop(self):
        if self.process and self.process.returncode is None:
            self.state = "stopping"
            self.process.terminate()
            try:
                await asyncio.wait_for(
                    self.process.wait(), self.settings.runtime.shutdown_timeout + 10
                )
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.monitor:
            await self.monitor
        self.process = None
        self.state = "stopped"
        self.message = "机器人已停止。"

    async def _start(self, command="run"):
        if self.process and self.process.returncode is None:
            raise ConsoleError("请先停止当前任务。")
        if command == "run" and (not self.settings.ai_ready):
            raise ConsoleError("请选择本地模型，或填写 API 模型名称和密钥。")
        if command == "run":
            from .ai.client import AIError
            from .ai.local import check_local_ready

            try:
                await asyncio.to_thread(check_local_ready, self.settings)
            except AIError:
                raise ConsoleError(
                    "本地推理尚未准备好：需要 Apple Silicon、local 依赖和已下载模型。"
                    "请查看「本地模型」的安装说明。"
                ) from None
        if command == "login":
            self.login_started = time.time()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "bili_comment_bot",
            "--config",
            str(self.path),
            command,
            "--headless",
            stdout=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.state = "login" if command == "login" else "running"
        self.message = "正在生成二维码…" if command == "login" else "机器人正在启动。"
        self.monitor = asyncio.create_task(self._watch(self.process, command))

    async def action(self, route, payload):
        async with self.lock:
            if self.closing:
                raise ConsoleError("控制台正在关闭，请稍后重新启动。")
            if route == "/api/config":
                if payload.get("revision") != self.revision:
                    raise ConsoleError("配置已更新，请重新加载页面后编辑。")
                raw = payload.get("settings")
                if not isinstance(raw, dict):
                    raise ConsoleError("配置格式无效。")
                for section in ("ai", "transcription"):
                    if isinstance(raw.get(section), dict) and raw[section].get("api_key") == "":
                        raw[section]["api_key"] = getattr(
                            self.settings, section
                        ).api_key.get_secret_value()
                for section in ("ai", "transcription"):
                    if isinstance(raw.get(section), dict) and raw[section].get("api_key") is None:
                        raw[section]["api_key"] = ""
                candidate = Settings.model_validate(raw)
                if self.state == "login":
                    raise ConsoleError("扫码期间请先停止登录，再保存配置。")
                restart = self.state == "running"
                # Stop fully before swapping settings; never mutate a live dispatcher.
                await self._stop()
                target = web_config_path(self.path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private(target, private_settings(candidate))
                self.settings = candidate
                self.revision += 1
                self.message = "配置已保存。"
                if restart:
                    try:
                        await self._start()
                    except ConsoleError:
                        self.message = "配置已保存；请补全模型和密钥后启动。"
            elif route == "/api/start":
                await self._start()
            elif route == "/api/stop":
                await self._stop()
            elif route == "/api/login":
                await self._stop()
                await self._start("login")
            else:
                raise ConsoleError("未知操作。")
            return self.snapshot()


def handler_for(console, loop, hosts):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def version_string(self):
            return "BotConsole"

        def log_message(self, *_):
            pass  # Never log Authorization, QR requests or configuration payloads.

        def reply(self, code, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'none'"
                ),
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in hosts or (origin and origin != f"http://{host}"):
                self.reply(403, {"error": "请求来源不被允许。"})
                return False
            if self.path.startswith("/api/") and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + console.token
            ):
                self.reply(401, {"error": "请使用启动日志中的控制台链接登录。"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == "/api/state":

                async def snapshot():
                    return console.snapshot()

                result = asyncio.run_coroutine_threadsafe(snapshot(), loop).result()
                self.reply(200, result)
            elif self.path == "/api/qr":
                path = console.settings.data_dir / "login.png"
                try:
                    if console.state != "login" or path.stat().st_mtime < console.login_started:
                        raise FileNotFoundError()
                    self.reply(200, path.read_bytes(), "image/png")
                except OSError:
                    self.reply(404, {"error": "二维码尚未准备好或已失效。"})
            elif self.path in ("/", "/app.js", "/style.css"):
                name, mime = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }[self.path]
                self.reply(200, (ASSETS / name).read_bytes(), mime)
            else:
                self.reply(404, {"error": "页面不存在。"})

        def do_POST(self):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 262144
                    or self.headers.get("Content-Type") != "application/json"
                ):
                    self.reply(400, {"error": "请发送有效的 JSON 配置。"})
                    return
                self.connection.settimeout(10)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ConsoleError("请求格式无效。")
                future = asyncio.run_coroutine_threadsafe(console.action(self.path, payload), loop)
                self.reply(200, future.result())
            except ValidationError as error:
                self.reply(
                    422,
                    {
                        "error": "配置校验失败",
                        "fields": [
                            {"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                            for item in error.errors(include_input=False, include_context=False)
                        ],
                    },
                )
            except ConsoleError as error:
                self.reply(400, {"error": str(error)})
            except (ValueError, TypeError):
                self.reply(400, {"error": "操作未完成，请检查字段、配置版本或当前任务状态。"})
            except Exception:
                self.reply(500, {"error": "操作未完成，请检查目录权限和服务状态。"})

    return Handler


async def serve(path, host="127.0.0.1", port=8765):
    if not 1 <= port <= 65535:
        raise ConsoleError("控制台端口必须为 1–65535。")
    console = Console(path)
    # Separate console lock: login and the worker retain their original data-directory lock.
    target = web_config_path(console.path).resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with InstanceLock(target.parent / ("." + target.name + ".console")):
        loop = asyncio.get_running_loop()
        hosts = {f"{name}:{port}" for name in ("localhost", "127.0.0.1", host)}
        server = ThreadingHTTPServer((host, port), handler_for(console, loop, hosts))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        print(f"配置控制台：http://{display_host}:{port}/#token={console.token}", flush=True)
        try:
            if console.settings.ai_ready and (console.settings.data_dir / "auth.json").exists():
                try:
                    await console._start()
                except ConsoleError as error:
                    console.state, console.message = "error", str(error)
            await stop.wait()
        finally:
            console.closing = True
            # Stop accepting requests before terminating the child.
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            async with console.lock:
                await console._stop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
