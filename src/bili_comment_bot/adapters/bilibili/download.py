"""Credential-free bounded downloads from explicitly researched CDN hosts."""

import asyncio
import ipaddress
import json
import logging
from urllib.parse import urlsplit

import httpx

from .collection import mapping
from .errors import HTTPFault, NetworkFault, ProtocolFault

SUBTITLE_HOSTS = {"aisubtitle.hdslb.com"}


def checked_url(value: str, hosts=SUBTITLE_HOSTS) -> str:
    if not isinstance(value, str) or any(char in value for char in "\\\r\n\t"):
        raise ProtocolFault()
    url = "https:" + value if value.startswith("//") else value
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or port not in {None, 443}
            or parts.fragment
            or host not in hosts
            or not parts.path.startswith("/")
        ):
            raise ProtocolFault()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ProtocolFault()
    except ValueError:
        raise ProtocolFault() from None
    return url


class Downloader:
    def __init__(self, timeout: float, max_bytes: int, transport=None):
        self.timeout, self.max_bytes = timeout, max_bytes
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def fetch(self, url: str, *, max_bytes: int | None = None, hosts=SUBTITLE_HOSTS) -> bytes:
        url = checked_url(url, hosts)
        limit = min(self.max_bytes, max_bytes) if max_bytes is not None else self.max_bytes
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    "GET",
                    url,
                    headers={
                        "Cookie": "",
                        "Accept-Encoding": "identity",
                        "Referer": "https://www.bilibili.com/",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise HTTPFault(response.status_code)
                    result = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        result.extend(chunk)
                        if len(result) > limit:
                            raise ProtocolFault()
                    return bytes(result)
        except (httpx.HTTPError, TimeoutError):
            raise NetworkFault() from None

    async def subtitle(self, url: str) -> dict:
        try:
            return mapping(json.loads(await self.fetch(url)))
        except (ValueError, UnicodeError):
            raise ProtocolFault() from None
