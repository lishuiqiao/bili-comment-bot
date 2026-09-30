"""Private, atomic credential state. The on-disk representation is the only plaintext export."""

import json
import os
import stat
import tempfile
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .errors import ProtocolFault, ReauthenticationRequired


class RefreshPhase(StrEnum):
    STABLE = "stable"
    REFRESH_STARTED = "refresh_started"
    CONFIRM_PENDING = "confirm_pending"
    CONFIRM_STARTED = "confirm_started"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    uid: int = Field(gt=0, strict=True)
    cookies: dict[str, SecretStr] = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    old_token: SecretStr = Field(default=SecretStr(""), repr=False)
    phase: RefreshPhase = RefreshPhase.STABLE

    @model_validator(mode="after")
    def required_secrets(self):
        for key in ("SESSDATA", "bili_jct", "DedeUserID"):
            if key not in self.cookies or not self.cookies[key].get_secret_value():
                raise ValueError("required login cookie missing")
        if self.cookies["DedeUserID"].get_secret_value() != str(self.uid):
            raise ValueError("cookie identity mismatch")
        if not self.refresh_token.get_secret_value():
            raise ValueError("refresh token missing")
        for key, value in self.cookies.items():
            if any(c in key + value.get_secret_value() for c in "\r\n;"):
                raise ValueError("invalid cookie")
        if self.phase in {RefreshPhase.CONFIRM_PENDING, RefreshPhase.CONFIRM_STARTED} and not (
            self.old_token.get_secret_value()
        ):
            raise ValueError("confirmation token missing")
        return self

    def cookie_values(self) -> dict[str, str]:
        return {key: value.get_secret_value() for key, value in self.cookies.items()}

    @property
    def csrf(self) -> str:
        return self.cookies["bili_jct"].get_secret_value()

    def require_stable(self):
        if self.phase != RefreshPhase.STABLE:
            raise ReauthenticationRequired()


class CredentialFile:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Credentials | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not stat.S_ISREG(self.path.stat().st_mode):
            raise ProtocolFault()
        if self.path.stat().st_mode & 0o077:
            raise PermissionError("credential file must have mode 0600")
        if self.path.stat().st_size > 65536:
            raise ProtocolFault()
        try:
            return Credentials.model_validate_json(self.path.read_bytes())
        except ValueError:
            raise ProtocolFault() from None

    def save(self, credentials: Credentials):
        folder = self.path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.is_symlink() or self.path.is_symlink():
            raise PermissionError("credential paths cannot be symlinks")
        os.chmod(folder, 0o700)
        payload = {
            "uid": credentials.uid,
            "cookies": credentials.cookie_values(),
            "refresh_token": credentials.refresh_token.get_secret_value(),
            "old_token": credentials.old_token.get_secret_value(),
            "phase": credentials.phase,
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=folder)
        try:
            with os.fdopen(descriptor, "w") as file:
                os.fchmod(file.fileno(), 0o600)
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
