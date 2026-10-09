"""Serialized, offline native inference in disposable processes.

Only model preparation downloads weights. Jobs never inherit bot credentials and
never fall back to a paid service. Process exit releases all model allocations.
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import platform
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from .client import AIError
from .local_errors import LOCAL_FAILURES
from .transcription import TranscriptionError, TranscriptResult, check_m4a


def local_failure(reason):
    reason = reason if reason in LOCAL_FAILURES else "local_inference_failed"
    # Business safety checks may consume AIError as UNKNOWN; retain a safe diagnosis.
    logging.getLogger(__name__).warning("Local inference failed: %s", reason)
    return AIError(reason)


class LocalRunner:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()

    async def run(self, operation, payload, media=None):
        async with self.lock:
            with tempfile.TemporaryDirectory(prefix="bili-local-") as directory:
                folder = Path(directory)
                request = {
                    "operation": operation,
                    "local": self.settings.local.model_dump(),
                    "vision": self.settings.vision.model_dump(),
                    "payload": payload,
                }
                (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
                if media is not None:
                    (folder / "media.mp4").write_bytes(media)
                # Deliberately omit cookies, service keys, proxy and provider environment.
                env = {
                    key: os.environ[key]
                    for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
                    if key in os.environ
                }
                env.update(
                    HF_HUB_OFFLINE="1",
                    TRANSFORMERS_OFFLINE="1",
                    HF_HUB_DISABLE_TELEMETRY="1",
                    TOKENIZERS_PARALLELISM="false",
                )
                process = None
                spawning = None
                try:
                    async with asyncio.timeout(self.settings.local.timeout):
                        spawning = asyncio.create_task(
                            asyncio.create_subprocess_exec(
                                sys.executable,
                                "-m",
                                "bili_comment_bot.ai.local_worker",
                                str(folder),
                                env=env,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                        )
                        process = await asyncio.shield(spawning)
                        await process.wait()
                        output = folder / "result.json"
                        if process.returncode or not output.exists():
                            reason = "local_inference_failed"
                            failure = folder / "error.json"
                            with contextlib.suppress(OSError, ValueError, TypeError):
                                if failure.stat().st_size <= 1024:
                                    error = json.loads(failure.read_text(encoding="utf-8"))
                                    if (
                                        isinstance(error, dict)
                                        and isinstance(error.get("reason"), str)
                                        and error["reason"] in LOCAL_FAILURES
                                    ):
                                        reason = error["reason"]
                            raise local_failure(reason)
                        if output.stat().st_size > 4_000_000:
                            raise local_failure("local_response_budget")
                        return json.loads(output.read_text(encoding="utf-8"))
                except TimeoutError:
                    raise local_failure("local_inference_timeout") from None
                except (OSError, ValueError):
                    raise local_failure("local_inference_unavailable") from None
                finally:
                    if process is None and spawning is not None:
                        # Cancellation must not lose a process created just after the await.
                        with contextlib.suppress(Exception):
                            process = await spawning
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.wait()


class LocalTranscriber:
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner
        self.calls = deque()
        self.metrics = {"requests": 0, "failures": 0, "audio_seconds": 0.0}

    async def close(self):
        pass

    async def transcribe(self, audio, expected_duration):
        check_m4a(audio)
        if (
            len(audio) > self.settings.transcription.max_upload_bytes
            or type(expected_duration) not in {int, float}
            or not math.isfinite(expected_duration)
            or not 0 < expected_duration <= self.settings.evidence.max_video_seconds
        ):
            raise TranscriptionError("local_audio_budget")
        now = time.monotonic()
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()
        if len(self.calls) >= self.settings.transcription.max_calls_per_minute:
            raise TranscriptionError("transcription_request_budget")
        self.calls.append(now)
        self.metrics["requests"] += 1
        try:
            body = await self.runner.run(
                "transcribe",
                {
                    "duration": expected_duration,
                    "language": self.settings.transcription.language,
                },
                audio,
            )
            result = TranscriptResult.model_validate(body)
            if abs(result.duration - expected_duration) > 2:
                raise TranscriptionError("transcription_duration_mismatch")
            if len(result.text) > self.settings.transcription.max_text_chars:
                raise TranscriptionError("transcription_text_budget")
            self.metrics["audio_seconds"] += result.duration
            return result
        except ValueError:
            self.metrics["failures"] += 1
            raise TranscriptionError("invalid_local_transcription") from None
        except AIError:
            self.metrics["failures"] += 1
            raise


def check_local_ready(settings):
    """Cheap preflight, no MLX import/GPU allocation and no network access."""
    import importlib.util

    speech = (
        settings.evidence.transcription_enabled and settings.transcription.backend == "local_mlx"
    )
    vision = settings.vision.enabled or settings.ai.backend == "local_mlx"
    if not (speech or vision):
        return
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AIError("local_requires_apple_silicon")
    packages = ["huggingface_hub", "av", "mlx"]
    packages += (["mlx_whisper"] if speech else []) + (["mlx_vlm"] if vision else [])
    if any(importlib.util.find_spec(package) is None for package in packages):
        raise AIError("local_dependencies_missing")
    from huggingface_hub import snapshot_download

    models = ([settings.local.model] if vision else []) + (
        [settings.local.speech_model] if speech else []
    )
    for model in models:
        try:
            folder = Path(snapshot_download(model, local_files_only=True))
            weights = list(folder.glob("*.safetensors")) + list(folder.glob("*.npz"))
            if not weights or not (folder / "config.json").is_file():
                raise ValueError("missing weights")
            for index in folder.glob("*.safetensors.index.json"):
                shards = json.loads(index.read_text())["weight_map"].values()
                if any(not (folder / name).is_file() for name in shards):
                    raise ValueError("incomplete weights")
        except Exception:
            raise AIError("local_models_not_prepared") from None


def prepare_models(settings):
    """Explicit download command; inference always resolves the offline cache."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Apple Silicon required")
    from huggingface_hub import snapshot_download

    for model in (settings.local.model, settings.local.speech_model):
        snapshot_download(
            model,
            allow_patterns=[
                "*.json",
                "*.safetensors",
                "*.npz",
                "*.txt",
                "*.tiktoken",
                "*.jinja",
                "*.model",
            ],
        )
