"""Private aggregate snapshots and allowlisted structured logs."""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .adapters.bilibili.errors import AUTH_FAILURES, PlatformError
from .ai.client import AIError

JOBS = {"at", "dm", "search", "events", "actions", "discovery", "refresh", "status", "runtime"}


def error_category(error):
    if isinstance(error, AUTH_FAILURES):
        return type(error).__name__
    for kind in (AIError, PlatformError, OSError, ValueError, RuntimeError):
        if isinstance(error, kind):
            return kind.__name__
    return "unexpected"


def log_result(
    job: str, result: str, mode: str, *, error=None, duration=0.0, backoff=0.0, counts=None
):
    if (
        job not in JOBS
        or result not in {"success", "failed", "stopped", "starting"}
        or mode not in {"sim", "live"}
    ):
        raise ValueError("invalid log fields")
    record = {
        "job": job,
        "result": result,
        "mode": mode,
        "duration_seconds": round(duration, 3),
        "backoff_seconds": backoff,
    }
    if counts is not None:
        if set(counts) != {"completed", "skipped", "deferred", "failed", "attention"} or any(
            type(value) is not int or value < 0 for value in counts.values()
        ):
            raise ValueError("invalid outcome counts")
        record["counts"] = dict(counts)
    if error is not None:
        record["error"] = error_category(error)
    logging.getLogger("bili_comment_bot.runtime").info(json.dumps(record, sort_keys=True))


def write_private(path: Path, payload: dict):
    descriptor, filename = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(payload, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def read_status(directory: Path, namespace: str, *, now=time.time) -> dict:
    with (directory / f"status-{namespace}.json").open() as file:
        value = json.load(file)
    if value.get("mode") != namespace:
        raise ValueError("status namespace mismatch")
    value["stale"] = now() - value["updated_at"] > value["stale_after"]
    value["healthy"] = (
        value["alive"]
        and value["ready"]
        and not value["stale"]
        and value.get("business_health") == "normal"
    )
    return value
