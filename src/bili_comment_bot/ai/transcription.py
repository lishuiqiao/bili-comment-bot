"""Bounded, credential-isolated multipart transcription; never an implicit retry."""

import asyncio
import json
import logging
import math
import re
import time
from collections import deque

import httpx
from pydantic import Field, ValidationError, model_validator

from ..config import Settings
from .client import AIError
from .contracts import AIContract


class TranscriptionError(AIError):
    pass


class TranscriptSegment(AIContract):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=200000)


class TranscriptResult(AIContract):
    speech_present: bool = True
    text: str = Field(max_length=200000)
    language: str = Field(pattern=r"^[A-Za-z-]{1,80}$")
    duration: float = Field(gt=0, allow_inf_nan=False)
    segments: list[TranscriptSegment] = Field(max_length=20000)

    @model_validator(mode="after")
    def ordered_segments(self):
        if not self.speech_present:
            if self.text or self.segments:
                raise ValueError("silence cannot contain speech")
            return self
        if not self.segments:
            raise ValueError("speech requires segments")
        previous = -1.0
        for segment in self.segments:
            if (
                segment.start < previous
                or segment.end <= segment.start
                or segment.end > self.duration + 0.1
                or not segment.text.strip()
            ):
                raise ValueError("invalid transcript timestamps")
            previous = segment.start
        joined = "".join(segment.text for segment in self.segments)
        if not self.text.strip() or re.sub(r"\s", "", joined) != re.sub(r"\s", "", self.text):
            raise ValueError("text and timed segments must agree")
        return self


def check_m4a(audio: bytes):
    """Reject truncated/non-MP4 input; the provider decodes and reports actual duration."""
    offset, boxes, count = 0, set(), 0
    while offset < len(audio):
        if len(audio) - offset < 8 or count > 50000:
            raise TranscriptionError("invalid_audio_container")
        size = int.from_bytes(audio[offset : offset + 4], "big")
        kind = audio[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if len(audio) - offset < 16:
                raise TranscriptionError("invalid_audio_container")
            size, header = int.from_bytes(audio[offset + 8 : offset + 16], "big"), 16
        if size == 0:
            size = len(audio) - offset
        if size < header or offset + size > len(audio):
            raise TranscriptionError("invalid_audio_container")
        if count == 0 and kind != b"ftyp":
            raise TranscriptionError("invalid_audio_container")
        boxes.add(kind)
        offset += size
        count += 1
    if not {b"ftyp", b"moov", b"mdat"} <= boxes:
        raise TranscriptionError("invalid_audio_container")


class TranscriptionClient:
    def __init__(self, settings: Settings, transport=None):
        config = settings.transcription
        if not config.api_key.get_secret_value():
            raise TranscriptionError("transcription_configuration_missing")
        if config.base_url.startswith("http:") and not config.allow_insecure_http:
            raise TranscriptionError("insecure_transcription_endpoint")
        self.settings = settings
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.calls = deque()
        self.metrics = {"requests": 0, "failures": 0, "audio_seconds": 0.0}
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def transcribe(self, audio: bytes, expected_duration: float) -> TranscriptResult:
        config = self.settings.transcription
        if (
            type(expected_duration) not in {int, float}
            or not math.isfinite(expected_duration)
            or not 0 < expected_duration <= self.settings.evidence.max_video_seconds
        ):
            raise TranscriptionError("invalid_expected_duration")
        if len(audio) > config.max_upload_bytes:
            raise TranscriptionError("transcription_upload_budget")
        check_m4a(audio)
        data = {
            "model": self.settings.evidence.transcription_model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if config.language:
            data["language"] = config.language
        try:
            async with asyncio.timeout(config.timeout):
                async with self.semaphore:
                    now = time.monotonic()
                    while self.calls and self.calls[0] <= now - 60:
                        self.calls.popleft()
                    if len(self.calls) >= config.max_calls_per_minute:
                        raise TranscriptionError("transcription_request_budget")
                    self.calls.append(now)
                    self.metrics["requests"] += 1
                    async with self.client.stream(
                        "POST",
                        config.base_url + "/audio/transcriptions",
                        data=data,
                        files={"file": ("audio.m4a", audio, "audio/mp4")},
                        headers={
                            "Authorization": "Bearer " + config.api_key.get_secret_value(),
                            "Cookie": "",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        if response.status_code != 200:
                            raise TranscriptionError("transcription_http", response.status_code)
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            raw.extend(chunk)
                            if len(raw) > config.max_response_bytes:
                                raise TranscriptionError("transcription_response_budget")
                    try:
                        body = json.loads(raw)
                        if "task" in body and body["task"] != "transcribe":
                            raise ValueError("unexpected transcription task")
                        # Provider diagnostics are not trusted fields or durable evidence.
                        selected = {key: body[key] for key in ("text", "language", "duration")}
                        selected["segments"] = [
                            {key: segment[key] for key in ("start", "end", "text")}
                            for segment in body["segments"]
                        ]
                        result = TranscriptResult.model_validate(selected)
                    except (ValueError, TypeError, KeyError, UnicodeError, ValidationError):
                        raise TranscriptionError("invalid_transcription_response") from None
                    if abs(result.duration - expected_duration) > 2:
                        raise TranscriptionError("transcription_duration_mismatch")
                    if len(result.text) > config.max_text_chars:
                        raise TranscriptionError("transcription_text_budget")
                    self.metrics["audio_seconds"] += result.duration
                    return result
        except (httpx.HTTPError, TimeoutError):
            self.metrics["failures"] += 1
            raise TranscriptionError("transcription_network_or_timeout") from None
        except AIError:
            self.metrics["failures"] += 1
            raise
