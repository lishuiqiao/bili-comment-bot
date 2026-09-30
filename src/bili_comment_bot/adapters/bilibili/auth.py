"""QR login and crash-aware Cookie rotation. No CAPTCHA bypass or password storage."""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import qrcode
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import BaseModel, Field, SecretStr

from ...config import Settings
from .auth_state import CredentialFile, Credentials, RefreshPhase
from .errors import IdentityMismatch, LoginExpired, ProtocolFault, ReauthenticationRequired
from .transport import BiliTransport

# Public protocol key, also published by bilibili-api-python 17.4.2.
REFRESH_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


def report_auth_failure(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except BaseException as error:
            self._report(error)
            raise

    return wrapped


def correspond_path(timestamp_ms: int) -> str:
    key = serialization.load_pem_public_key(REFRESH_PUBLIC_KEY)
    return key.encrypt(
        f"refresh_{timestamp_ms}".encode(),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).hex()


class RefreshCSRFParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        if tag == "div" and dict(attrs).get("id") == "1-name":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "div":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.tokens.append(data.strip())


def refresh_csrf(html: bytes) -> str:
    parser = RefreshCSRFParser()
    try:
        parser.feed(html.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ProtocolFault() from None
    if len(parser.tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", parser.tokens[0]):
        raise ProtocolFault()
    return parser.tokens[0]


class QRStatus(StrEnum):
    WAITING_SCAN = "waiting_scan"
    WAITING_CONFIRM = "waiting_confirm"
    EXPIRED = "expired"
    SUCCEEDED = "succeeded"


class QRChallenge(BaseModel):
    key: SecretStr = Field(repr=False)
    url: SecretStr = Field(repr=False)

    def write_image(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise PermissionError("QR image cannot be a symlink")
        os.chmod(path.parent, 0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            qrcode.make(self.url.get_secret_value()).save(file, format="PNG")


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        transport: BiliTransport,
        file: CredentialFile,
        identity_guard: Callable[[int], Awaitable[None]] | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self.file = file
        self.state = file.load()
        self.lock = asyncio.Lock()
        self.identity_guard = identity_guard

    def _report(self, error):
        fault = self.transport.auth_fault
        if fault:
            fault.notify(error)
            if self.state and self.state.phase in {
                RefreshPhase.REFRESH_STARTED,
                RefreshPhase.CONFIRM_STARTED,
            }:
                fault.notify(ReauthenticationRequired())

    def _save(self, state: Credentials):
        self.file.save(state)
        self.state = state

    @report_auth_failure
    async def _verify(self, state: Credentials) -> dict:
        data = (
            await self.transport.request(
                "GET", "api", "/x/web-interface/nav", cookies=state.cookie_values()
            )
        ).data()
        if data.get("isLogin") is not True:
            raise LoginExpired()
        if type(data.get("mid")) is not int or data["mid"] != state.uid:
            raise IdentityMismatch()
        expected = self.settings.platform.bot_uid
        if expected and expected != state.uid:
            raise IdentityMismatch()
        if self.identity_guard:
            try:
                await self.identity_guard(state.uid)
            except ValueError:
                raise IdentityMismatch() from None
        return data

    @asynccontextmanager
    async def credentials(self):
        # Hold across a platform operation so its Cookie and CSRF cannot span two generations.
        async with self.lock:
            try:
                if self.transport.auth_fault:
                    self.transport.auth_fault.check()
                if self.state is None:
                    raise LoginExpired()
                self.state.require_stable()
                yield self.state
            except BaseException as error:
                self._report(error)
                raise

    async def verify(self) -> dict:
        async with self.credentials() as state:
            return await self._verify(state)

    async def generate_qr(self) -> QRChallenge:
        async with self.lock:
            data = (
                await self.transport.request(
                    "GET",
                    "passport",
                    "/x/passport-login/web/qrcode/generate",
                    params={"source": "main-fe-header"},
                )
            ).data()
            url, key = data.get("url"), data.get("qrcode_key")
            if not isinstance(url, str) or not isinstance(key, str) or not key:
                raise ProtocolFault()
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != "passport.bilibili.com":
                raise ProtocolFault()
            return QRChallenge(key=key, url=url)

    async def poll_qr(self, challenge: QRChallenge) -> QRStatus:
        async with self.lock:
            packet = await self.transport.request(
                "GET",
                "passport",
                "/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": challenge.key.get_secret_value(), "source": "main-fe-header"},
            )
            data = packet.data()
            code = data.get("code")
            if type(code) is not int:
                raise ProtocolFault()
            status = {
                86101: QRStatus.WAITING_SCAN,
                86090: QRStatus.WAITING_CONFIRM,
                86038: QRStatus.EXPIRED,
            }
            if code in status:
                return status[code]
            if code != 0:
                from .errors import business_error

                raise business_error(code)
            cookies = packet.cookies.copy()
            # Some web poll implementations deliver credentials in a return URL, not Set-Cookie.
            if not all(name in cookies for name in ("SESSDATA", "bili_jct", "DedeUserID")) and (
                isinstance(data.get("url"), str)
            ):
                parts = urlsplit(data["url"])
                if parts.scheme != "https" or parts.hostname != "passport.bilibili.com":
                    raise ProtocolFault()
                query = parse_qs(parts.query)
                for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                    if name not in cookies and name in query:
                        cookies[name] = query[name][0]
            try:
                state = Credentials(
                    uid=int(cookies["DedeUserID"]),
                    cookies=cookies,
                    refresh_token=data["refresh_token"],
                )
            except (KeyError, ValueError, TypeError):
                raise ProtocolFault() from None
            await self._verify(state)
            self._save(state)
            return QRStatus.SUCCEEDED

    @report_auth_failure
    async def refresh(self) -> bool:
        async with self.lock:
            state = self.state
            if state is None:
                raise LoginExpired()
            if state.phase in {RefreshPhase.REFRESH_STARTED, RefreshPhase.CONFIRM_STARTED}:
                # Both POSTs have side effects and no documented idempotent recovery contract.
                raise ReauthenticationRequired()
            if state.phase == RefreshPhase.CONFIRM_PENDING:
                await self._verify(state)
                await self._confirm(state)
                return True
            await self._verify(state)
            data = (
                await self.transport.request(
                    "GET",
                    "passport",
                    "/x/passport-login/web/cookie/info",
                    params={"csrf": state.csrf},
                    cookies=state.cookie_values(),
                )
            ).data()
            if type(data.get("refresh")) is not bool:
                raise ProtocolFault()
            if not data["refresh"]:
                return False
            timestamp = data.get("timestamp")
            if type(timestamp) is not int or timestamp <= 0:
                raise ProtocolFault()
            packet = await self.transport.request(
                "GET",
                "web",
                "/correspond/1/" + correspond_path(timestamp),
                cookies=state.cookie_values(),
            )
            token = refresh_csrf(packet.body)
            self._save(state.model_copy(update={"phase": RefreshPhase.REFRESH_STARTED}))
            packet = await self.transport.request(
                "POST",
                "passport",
                "/x/passport-login/web/cookie/refresh",
                cookies=state.cookie_values(),
                data={
                    "csrf": state.csrf,
                    "refresh_csrf": token,
                    "source": "main_web",
                    "refresh_token": state.refresh_token.get_secret_value(),
                },
            )
            data = packet.data()
            if data.get("status", 0) != 0:
                raise ProtocolFault()
            if not all(name in packet.cookies for name in ("SESSDATA", "bili_jct", "DedeUserID")):
                raise ProtocolFault()
            try:
                new = Credentials(
                    uid=state.uid,
                    cookies=state.cookie_values() | packet.cookies,
                    refresh_token=data["refresh_token"],
                    old_token=state.refresh_token,
                    phase=RefreshPhase.CONFIRM_PENDING,
                )
            except (ValueError, TypeError, KeyError):
                raise ProtocolFault() from None
            self._save(new)
            await self._verify(new)
            await self._confirm(new)
            return True

    async def _confirm(self, state: Credentials):
        self._save(state.model_copy(update={"phase": RefreshPhase.CONFIRM_STARTED}))
        packet = await self.transport.request(
            "POST",
            "passport",
            "/x/passport-login/web/confirm/refresh",
            cookies=state.cookie_values(),
            data={"csrf": state.csrf, "refresh_token": state.old_token.get_secret_value()},
        )
        packet.envelope()
        self._save(
            state.model_copy(update={"phase": RefreshPhase.STABLE, "old_token": SecretStr("")})
        )
