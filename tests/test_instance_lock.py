import asyncio
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from bili_comment_bot.config import Settings
from bili_comment_bot.instance_lock import InstanceInUse, InstanceLock

WORKER = """
import sys
from pathlib import Path
from bili_comment_bot.instance_lock import InstanceLock, InstanceInUse
try:
    with InstanceLock(Path(sys.argv[1])):
        print('ENTERED', flush=True)
        if sys.stdin.readline().strip() == 'error':
            raise RuntimeError('injected exit')
except InstanceInUse:
    print('BUSY', flush=True)
    sys.exit(2)
"""


def worker(path):
    # No HTTP code in the child; fixture network bans remain intact in the pytest process.
    return subprocess.Popen(
        [sys.executable, "-c", WORKER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )


def ready(process):
    assert select.select([process.stdout], [], [], 15)[0], "child did not signal readiness"
    return process.stdout.readline().strip()


def test_two_processes_compete_and_different_directories_do_not_block(tmp_path):
    owner = worker(tmp_path / "one")
    blocked = independent = None
    try:
        assert ready(owner) == "ENTERED"
        blocked = worker(tmp_path / "one")
        assert ready(blocked) == "BUSY"
        assert blocked.wait(timeout=15) == 2
        independent = worker(tmp_path / "two")
        assert ready(independent) == "ENTERED"
        independent.communicate("exit\n", timeout=15)
        assert independent.returncode == 0
        owner.communicate("exit\n", timeout=15)
        assert owner.returncode == 0
        with InstanceLock(tmp_path / "one"):
            pass
    finally:
        for process in (owner, blocked, independent):
            if process:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=15)


def test_equivalent_and_symlink_paths_cannot_bypass_lock(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with InstanceLock(directory):
        for path in (directory / ".." / "data", alias):
            with pytest.raises(InstanceInUse):
                with InstanceLock(path):
                    pytest.fail("entered already locked directory")


@pytest.mark.parametrize("exit_mode", ["error", "kill"])
def test_exception_and_process_death_release_kernel_lock(tmp_path, exit_mode):
    owner = worker(tmp_path)
    try:
        assert ready(owner) == "ENTERED"
        inode = (tmp_path / ".instance.lock").stat().st_ino
        if exit_mode == "kill":
            owner.kill()
            owner.communicate(timeout=15)
        else:
            owner.communicate("error\n", timeout=15)
        assert owner.returncode != 0
        with InstanceLock(tmp_path):
            assert (tmp_path / ".instance.lock").stat().st_ino == inode
        assert (tmp_path / ".instance.lock").exists()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=15)


@pytest.mark.parametrize("command", ["login", "refresh-auth", "auth-status"])
async def test_busy_auth_command_never_loads_credentials_or_touches_database(
    command, tmp_path, monkeypatch
):
    from bili_comment_bot import __main__ as cli

    calls = []

    async def should_not_enter(*args):
        calls.append("loaded credentials / HTTP")

    monkeypatch.setattr(cli, "_auth_command_locked", should_not_enter)
    auth = tmp_path / "auth.json"
    db = tmp_path / "state.db"
    auth.write_bytes(b"private sentinel")
    db.write_bytes(b"database sentinel")
    with InstanceLock(tmp_path):
        with pytest.raises(InstanceInUse):
            await cli.auth_command(Settings(data_dir=tmp_path), command)
    assert not calls
    assert auth.read_bytes() == b"private sentinel"
    assert db.read_bytes() == b"database sentinel"


async def test_cancellation_releases_auth_lifecycle_lock(tmp_path, monkeypatch):
    from bili_comment_bot import __main__ as cli

    entered = asyncio.Event()

    async def wait_forever(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "_auth_command_locked", wait_forever)
    task = asyncio.create_task(cli.auth_command(Settings(data_dir=tmp_path), "login"))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with InstanceLock(tmp_path):
            pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
