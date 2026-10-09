"""Regression coverage for bounded resource use and private local diagnostics."""

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from bili_comment_bot.adapters.bilibili.download import Downloader
from bili_comment_bot.adapters.bilibili.errors import ProtocolFault
from bili_comment_bot.ai.client import AIError
from bili_comment_bot.ai.local import LocalRunner
from bili_comment_bot.ai.local_errors import LocalWorkerError
from bili_comment_bot.ai.local_worker import main as worker_main
from bili_comment_bot.config import Settings
from bili_comment_bot.observability import log_result
from bili_comment_bot.storage import Store


async def test_expired_cache_housekeeping_is_bounded_indexed_and_mode_isolated(tmp_path):
    store = await Store(tmp_path / "state.db", namespace="sim", clock=lambda: 100).open()
    try:
        async with store.transaction() as db:
            await db.executemany(
                "INSERT INTO cache VALUES(?,?,?,?)",
                [("sim", f"expired-{i}", "{}", 99) for i in range(250)],
            )
            await db.execute("INSERT INTO cache VALUES('live','expired','{}',99)")
            await db.execute("INSERT INTO cache VALUES('sim','fresh','{}',200)")
            plan = await (
                await db.execute(
                    "EXPLAIN QUERY PLAN SELECT key FROM cache WHERE ns=? AND expires<=? "
                    "ORDER BY expires,key LIMIT 100",
                    ("sim", 100),
                )
            ).fetchall()
            assert any("cache_expiry" in row[3] for row in plan)
        for expected in (150, 50, 0):
            await store.cache_put("new", {"value": "retained"}, 20)
            async with store.transaction() as db:
                remaining = await (
                    await db.execute("SELECT COUNT(*) FROM cache WHERE ns='sim' AND expires<=100")
                ).fetchone()
                assert remaining[0] == expected
        assert await store.cache_get("fresh") == {}
        assert await store.cache_get("new") == {"value": "retained"}
        async with store.transaction() as db:
            assert (
                await (await db.execute("SELECT COUNT(*) FROM cache WHERE ns='live'")).fetchone()
            )[0] == 1
    finally:
        await store.close()


async def test_exhausted_download_budget_sends_no_request():
    calls = []
    client = Downloader(1, 100, httpx.MockTransport(lambda r: calls.append(r)))
    try:
        for budget in (0, -1):
            with pytest.raises(ProtocolFault):
                await client.fetch("https://aisubtitle.hdslb.com/a", max_bytes=budget)
        assert not calls
    finally:
        await client.close()


async def test_oversized_declared_length_stops_before_reading_stream():
    class Stream(httpx.AsyncByteStream):
        consumed = False

        async def __aiter__(self):
            self.consumed = True
            yield b"oversized"

    stream = Stream()
    client = Downloader(
        1,
        10,
        httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"Content-Length": "100"}, stream=stream)
        ),
    )
    try:
        with pytest.raises(ProtocolFault):
            await client.fetch("https://aisubtitle.hdslb.com/a")
        assert not stream.consumed
    finally:
        await client.close()


@pytest.mark.parametrize("length", [None, "5", "invalid"])
async def test_actual_download_size_still_checked_when_length_missing_or_wrong(length):
    headers = {} if length is None else {"Content-Length": length}
    client = Downloader(
        1,
        10,
        httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, content=b"x" * 11)),
    )
    try:
        with pytest.raises(ProtocolFault):
            await client.fetch("https://aisubtitle.hdslb.com/a")
    finally:
        await client.close()


async def test_cancel_during_process_creation_does_not_orphan_model(monkeypatch):
    entered, finish_spawn, reaped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    folders = []

    class Process:
        returncode = None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            assert self.returncode == -9
            reaped.set()

    process = Process()

    async def spawn(*args, **_):
        folders.append(Path(args[-1]))
        entered.set()
        await finish_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(LocalRunner(Settings()).run("complete", {}))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # Cleanup owns the still-creating subprocess.
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reaped.is_set() and not folders[0].exists()


@pytest.mark.parametrize("reason", ["local_context_budget", "private-fixture-secret", ["bad"]])
async def test_worker_failure_only_returns_allowlisted_diagnostics(monkeypatch, reason, caplog):
    async def spawn(*args, **_):
        (Path(args[-1]) / "error.json").write_text(json.dumps({"reason": reason}))
        process = AsyncMock()
        process.returncode = 1
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AIError) as error:
        await LocalRunner(Settings()).run("complete", {})
    expected = (
        "local_context_budget" if reason == "local_context_budget" else "local_inference_failed"
    )
    assert error.value.reason == expected and "private-fixture-secret" not in str(error.value)
    assert expected in caplog.text and "private-fixture-secret" not in caplog.text


@pytest.mark.parametrize(
    "error,reason",
    [
        (LocalWorkerError("local_context_budget"), "local_context_budget"),
        (ValueError("private-fixture-secret"), "local_inference_failed"),
        (ImportError("private-fixture-secret"), "local_dependencies_missing"),
        (MemoryError("private-fixture-secret"), "local_memory_budget"),
    ],
)
def test_worker_writes_only_fixed_errors(tmp_path, monkeypatch, error, reason):
    (tmp_path / "request.json").write_text("{}")
    monkeypatch.setattr("sys.argv", ["local_worker", str(tmp_path)])
    monkeypatch.setattr("bili_comment_bot.ai.local_worker.os.umask", lambda _: None)

    def execute(*_):
        raise error

    monkeypatch.setattr("bili_comment_bot.ai.local_worker.execute", execute)
    with pytest.raises(SystemExit) as result:
        worker_main()
    assert result.value.code == 1
    assert json.loads((tmp_path / "error.json").read_text()) == {"reason": reason}
    assert not (tmp_path / "result.json").exists()


def test_logs_allow_safe_local_reason_without_raw_error(caplog):
    with caplog.at_level(logging.INFO, logger="bili_comment_bot.runtime"):
        log_result("events", "failed", "sim", error=AIError("local_context_budget"))
        log_result("events", "failed", "sim", error=AIError("private-fixture-secret"))
    rows = [json.loads(row.message) for row in caplog.records]
    assert rows[0]["error_reason"] == "local_context_budget"
    assert "error_reason" not in rows[1] and "private-fixture-secret" not in caplog.text
