"""Real Docker CLI smoke checks. Every runtime container has networking disabled."""

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

IMAGE = "bili-comment-bot:ci"
volume = "bili-bot-ci-" + uuid.uuid4().hex


def run(*args, expected=0):
    result = subprocess.run(args, text=True, capture_output=True)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


run("docker", "volume", "create", volume)
try:
    metadata = json.loads(run("docker", "image", "inspect", IMAGE))[0]["Config"]
    assert metadata["User"] == "10001:10001"
    assert metadata["Entrypoint"] == ["bili-comment-bot", "--config", "/app/config.toml"]
    base = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:size=32m,mode=1777",
        "--mount",
        f"type=volume,source={volume},target=/data",
    ]
    run(*base, IMAGE, "config-check")
    assert json.loads(run(*base, IMAGE, "demo"))["platform_write_calls"] == 0
    assert json.loads(run(*base, IMAGE, "evaluate"))["model_calls"] == 0
    # No credentials and no networking: fail closed, without publishing or hidden fallback.
    run(
        *base,
        "-e",
        "BILI_BOT_AI_MODEL=ci-placeholder",
        "-e",
        "BILI_BOT_AI_API_KEY=ci-placeholder",
        IMAGE,
        "run",
        "--once",
        expected=2,
    )
    code = """import os, pathlib, importlib.util
assert os.getuid() == 10001
assert importlib.util.find_spec('pytest') is None
assert importlib.util.find_spec('ruff') is None
p=pathlib.Path('/data/probe'); p.write_text('persistent'); os.chmod(p,0o600)
assert pathlib.Path('/data').stat().st_mode & 0o777 == 0o700
"""
    run(*base, "--entrypoint", "python", IMAGE, "-c", code)
    run(
        *base,
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        "from pathlib import Path; assert Path('/data/probe').read_text() == 'persistent'",
    )
    probe = "from bili_comment_bot.healthcheck import main; main()"
    run(*base, "--entrypoint", "python", IMAGE, "-c", probe, expected=1)
    for namespace in ["sim", "live"]:
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            live = namespace == "live"
            config.write_text(
                "[publishing]\n"
                + f"dry_run={str(not live).lower()}\n"
                + f"publish_enabled={str(live).lower()}\n"
            )
            custom = [
                *base,
                "--mount",
                f"type=bind,source={config},target=/app/config.toml,readonly",
            ]
            status = f"""import time
from pathlib import Path
from bili_comment_bot.observability import write_private
write_private(Path('/data/status-{namespace}.json'), {{'mode':'{namespace}',
'updated_at':time.time(),'stale_after':60,'alive':True,'ready':True,'business_health':'normal'}})
"""
            run(*custom, "--entrypoint", "python", IMAGE, "-c", status)
            run(*custom, "--entrypoint", "python", IMAGE, "-c", probe)
    print(
        "Docker nonroot, persistence, fail-closed startup, packaged eval and local health passed."
    )
finally:
    run("docker", "volume", "rm", volume)
