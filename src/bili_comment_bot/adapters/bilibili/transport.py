"""Bounded fixed-origin HTTPS, with no automatic retries of POST requests."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from ...config import Settings
from .errors import (
    AuthFault,
    HTTPFault,
    NetworkFault,
    PlatformError,
    ProtocolFault,
    RateLimited,
    business_error,
)

ORIGINS = {
    "api": "https://api.bilibili.com",
    "passport": "https://passport.bilibili.com",
    "web": "https://www.bilibili.com",
    "message": "https://api.vc.bilibili.com",
}


class RateGate:
    def __init__(self, interval: float):
        self.interval = interval
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            delay = self.next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_at = time.monotonic() + self.interval


@dataclass(repr=False)
class Packet:
    body: bytes = field(repr=False)
    cookies: dict[str, str] = field(repr=False)
    auth_fault: AuthFault | None = field(default=None, repr=False)

    def envelope(self) -> dict:
        try:
            return self._envelope()
        except PlatformError as error:
            if self.auth_fault:
                self.auth_fault.notify(error)
            raise

    def _envelope(self) -> dict:
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeError):
            raise ProtocolFault() from None
        if not isinstance(value, dict) or type(value.get("code")) is not int:
            raise ProtocolFault()
        if value["code"] != 0:
            raise business_error(value["code"])
        data = value.get("data")
        if isinstance(data, dict) and data.get("v_voucher"):
            from .errors import CaptchaRequired

            raise CaptchaRequired()
        return value

    def data(self) -> dict:
        value = self.envelope().get("data")
        if not isinstance(value, dict):
            raise ProtocolFault()
        return value


class BiliTransport:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_response_bytes: int = 2_000_000,
        read_wait: Callable[[], Awaitable[None]] | None = None,
        write_wait: Callable[[], Awaitable[None]] | None = None,
        auth_fault: AuthFault | None = None,
    ):
        self.settings = settings
        self.auth_fault = auth_fault
        # httpx INFO messages include full URLs (QR keys/CSRF can be query parameters).
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.max_response_bytes = max_response_bytes
        self.read_wait = read_wait or RateGate(settings.platform.read_interval).wait
        self.write_wait = write_wait or RateGate(settings.platform.write_interval).wait
        self.semaphore = asyncio.Semaphore(settings.limits.concurrency)
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=settings.platform.request_timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=settings.limits.concurrency),
        )

    async def close(self):
        await self.client.aclose()

    async def request(
        self,
        method: str,
        origin: str,
        path: str,
        *,
        cookies: dict[str, str] | None = None,
        params: dict | None = None,
        data: dict | None = None,
        publish_guard: Callable[[], None] | None = None,
    ) -> Packet:
        if self.auth_fault:
            self.auth_fault.check()
        if (
            origin not in ORIGINS
            or method not in {"GET", "POST"}
            or not path.startswith("/")
            or path.startswith("//")
            or any(c in path for c in "?#\\\r\n")
        ):
            raise ProtocolFault()
        if cookies and any(
            any(c in key + value for c in "\r\n;") for key, value in cookies.items()
        ):
            raise ProtocolFault()
        # Explicit Cookie prevents httpx's accumulated jar from supplying another generation.
        headers = {
            "User-Agent": "Mozilla/5.0 bili-comment-bot/0.1",
            "Referer": "https://www.bilibili.com/",
            "Accept-Encoding": "identity",
            "Cookie": "; ".join(f"{key}={value}" for key, value in (cookies or {}).items()),
        }
        async with self.semaphore:
            await (self.write_wait() if method == "POST" else self.read_wait())
            if self.auth_fault:
                self.auth_fault.check()
            if publish_guard:
                publish_guard()
            request = self.client.build_request(
                method, ORIGINS[origin] + path, params=params, data=data, headers=headers
            )
            try:
                async with asyncio.timeout(self.settings.platform.request_timeout):
                    response = await self.client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            raise RateLimited(429)
                        if response.status_code != 200:
                            raise HTTPFault(response.status_code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            if len(body) > self.max_response_bytes:
                                raise ProtocolFault()
                        return Packet(bytes(body), dict(response.cookies.items()), self.auth_fault)
                    finally:
                        await response.aclose()
            except (httpx.HTTPError, TimeoutError):
                raise NetworkFault() from None
