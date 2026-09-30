"""Run with the independent wheel environment's Python from outside the repository."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import bili_comment_bot
from bili_comment_bot.evaluation import load_cases

repository = Path(sys.argv[1]).resolve()
package = Path(bili_comment_bot.__file__).resolve()
assert not package.is_relative_to(repository), package
assert len(load_cases()[0]) == 26
with tempfile.TemporaryDirectory(prefix="bili-bot-wheel-") as folder:
    config = repository / "config.example.toml"
    base = [sys.executable, "-m", "bili_comment_bot", "--config", str(config)]
    subprocess.run([*base, "config-check"], cwd=folder, check=True)
    demo = subprocess.run([*base, "demo"], cwd=folder, check=True, capture_output=True, text=True)
    assert json.loads(demo.stdout)["platform_write_calls"] == 0
    offline = subprocess.run(
        [*base, "evaluate"], cwd=folder, check=True, capture_output=True, text=True
    )
    report = json.loads(offline.stdout)
    assert report["http_requests"] == 0 and report["counts"]["not_evaluated"] > 0
print("Independent wheel: packaged dataset, config, demo and offline evaluation passed.")
