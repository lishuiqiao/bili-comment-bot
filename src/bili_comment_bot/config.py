"""Validated settings with legacy TOML import and private web-managed overrides."""

import json
import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Persona(ConfigModel):
    name: str = Field(default="B站评论机器人", min_length=1, max_length=30)
    personality: str = Field(default="温柔、真诚，有一点俏皮的日常陪伴者", max_length=1000)
    warmth: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    humor: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    empathy: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "@\r\n"):
            raise ValueError("bot name cannot be blank or contain @/newlines")
        return value


class PlatformConfig(ConfigModel):
    bot_uid: int = Field(default=0, ge=0, strict=True)
    request_timeout: float = Field(default=20, gt=0, le=120)
    read_interval: float = Field(default=1, ge=0.1)
    write_interval: float = Field(default=15, ge=1)
    poll_interval: float = Field(default=30, ge=5)
    refresh_interval: float = Field(default=1800, ge=60)
    max_pages: int = Field(default=20, ge=1, le=200)
    history_lookback_seconds: int = Field(default=3600, ge=0, le=604800)


class AIConfig(ConfigModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=100)
    api_key: SecretStr = SecretStr("")
    temperature: float = Field(default=0.7, ge=0, le=2, allow_inf_nan=False)
    timeout: float = Field(default=60, gt=0, le=300)
    max_tokens: int = Field(default=1800, ge=200, le=16000)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_completion_tokens"
    structured_output: Literal["json_object", "json_schema", "prompt"] = "json_object"
    send_temperature: bool = True
    retries: int = Field(default=0, ge=0, le=2)
    max_response_bytes: int = Field(default=1000000, ge=1000, le=4000000)
    max_calls_per_minute: int = Field(default=120, ge=1, le=10000)
    max_input_chars: int = Field(default=120000, ge=1000, le=500000)
    allow_insecure_http: bool = False

    @field_validator("base_url")
    @classmethod
    def provider_url(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c in value for c in "\\\r\n")
        ):
            raise ValueError(
                "AI base URL must be an HTTP(S) origin/path without credentials or query"
            )
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("invalid AI endpoint port") from None
        return value.rstrip("/")


class Limits(ConfigModel):
    dm_per_hour: int = Field(default=5, ge=0, le=10000)
    comment_per_hour: int = Field(default=5, ge=0, le=10000)
    whitelist: list[StrictInt] = Field(default_factory=list)
    concurrency: int = Field(default=3, ge=1, le=32)
    max_message_chars: int = Field(default=2000, ge=100, le=20000)
    max_reply_chars: int = Field(default=800, ge=50, le=2000)


class TranscriptionConfig(ConfigModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    language: str = Field(default="zh", pattern=r"^(?:[a-z]{2})?$", max_length=2)
    timeout: float = Field(default=120, gt=0, le=600)
    concurrency: int = Field(default=1, ge=1, le=8)
    max_upload_bytes: int = Field(default=24000000, ge=1000, le=25000000)
    max_response_bytes: int = Field(default=1000000, ge=1000, le=4000000)
    max_text_chars: int = Field(default=60000, ge=1000, le=200000)
    max_calls_per_video: int = Field(default=20, ge=1, le=200)
    max_calls_per_minute: int = Field(default=20, ge=1, le=1000)
    allow_insecure_http: bool = False
    backend_id: str = Field(default="openai-verbose-v1", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")

    @field_validator("base_url")
    @classmethod
    def provider_url(cls, value):
        return AIConfig.provider_url(value)


class Discovery(ConfigModel):
    keywords: list[str] = Field(default_factory=list, max_length=20)
    invite_uids: list[StrictInt] = Field(default_factory=list)
    interval: float = Field(default=1800, ge=60)
    pages_per_keyword: int = Field(default=1, ge=1, le=10)
    videos_per_cycle: int = Field(default=10, ge=1, le=100)

    @field_validator("keywords")
    @classmethod
    def bounded_keywords(cls, values):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("keywords must be nonempty and at most 200 characters")
        return list(dict.fromkeys(value.strip() for value in values))


class Publishing(ConfigModel):
    dry_run: bool = True
    publish_enabled: bool = False


class RuntimeConfig(ConfigModel):
    worker_interval: float = Field(default=2, ge=0.1, le=60)
    batch_size: int = Field(default=20, ge=1, le=100)
    shutdown_timeout: float = Field(default=30, gt=0, le=300)
    status_interval: float = Field(default=10, ge=1, le=60)


class EvidenceConfig(ConfigModel):
    cache_ttl: int = Field(default=3600, ge=60)
    max_video_seconds: int = Field(default=1800, ge=30, le=14400)
    max_download_mb: int = Field(default=64, ge=1, le=256)
    max_text_chars: int = Field(default=60000, ge=1000, le=200000)
    transcription_enabled: bool = False
    transcription_model: str = Field(default="whisper-1", min_length=1, max_length=100)
    subtitle_languages: list[str] = Field(
        default_factory=lambda: ["zh-CN", "ai-zh", "zh-Hans", "en"], min_length=1, max_length=10
    )
    comment_sample_size: int = Field(default=20, ge=1, le=100)


class Settings(ConfigModel):
    data_dir: Path = Path("data")
    persona: Persona = Field(default_factory=Persona)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    limits: Limits = Field(default_factory=Limits)
    discovery: Discovery = Field(default_factory=Discovery)
    publishing: Publishing = Field(default_factory=Publishing)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    unsafe_words: list[str] = Field(default_factory=list)

    @property
    def namespace(self) -> str:
        return "live" if self.publishing.publish_enabled and not self.publishing.dry_run else "sim"

    @field_validator("limits", "discovery")
    @classmethod
    def positive_uids(cls, value):
        uids = value.whitelist if isinstance(value, Limits) else value.invite_uids
        if any(isinstance(uid, bool) or uid <= 0 for uid in uids):
            raise ValueError("UIDs must be positive integers")
        return value


def load_settings(path: Path) -> Settings:
    saved = web_config_path(path)
    if saved.exists():
        with saved.open() as file:
            return Settings.model_validate(json.load(file))
    raw = {}
    if path.exists():
        with path.open("rb") as file:
            raw = tomllib.load(file)
    ai = raw.setdefault("ai", {})
    for env, key in (
        ("BILI_BOT_AI_BASE_URL", "base_url"),
        ("BILI_BOT_AI_MODEL", "model"),
        ("BILI_BOT_AI_API_KEY", "api_key"),
    ):
        if env in os.environ:
            ai[key] = os.environ[env]
    if "BILI_BOT_DATA_DIR" in os.environ:
        raw["data_dir"] = os.environ["BILI_BOT_DATA_DIR"]
    transcription = raw.setdefault("transcription", {})
    for env, key in (
        ("BILI_BOT_TRANSCRIPTION_BASE_URL", "base_url"),
        ("BILI_BOT_TRANSCRIPTION_API_KEY", "api_key"),
        ("BILI_BOT_TRANSCRIPTION_LANGUAGE", "language"),
    ):
        if env in os.environ:
            transcription[key] = os.environ[env]
    if "BILI_BOT_TRANSCRIPTION_MODEL" in os.environ:
        raw.setdefault("evidence", {})["transcription_model"] = os.environ[
            "BILI_BOT_TRANSCRIPTION_MODEL"
        ]
    return Settings.model_validate(raw)


def web_config_path(path: Path) -> Path:
    """Stable bootstrap location, independent of the editable data directory."""
    return Path(os.environ.get("BILI_BOT_WEB_CONFIG", str(path.with_suffix(".web.json"))))
