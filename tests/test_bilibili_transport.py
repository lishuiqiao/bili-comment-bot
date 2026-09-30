import asyncio
import logging

import httpx
import pytest

from bili_comment_bot.adapters.bilibili.errors import (
    CaptchaRequired,
    HTTPFault,
    LoginExpired,
    NetworkFault,
    ProtocolFault,
    RateLimited,
)
from bili_comment_bot.adapters.bilibili.transport import BiliTransport
from bili_comment_bot.config import Settings


async def no_wait():
    pass


def transport_for(handler, **kwargs):
    return BiliTransport(
        Settings(), httpx.MockTransport(handler), read_wait=no_wait, write_wait=no_wait, **kwargs
    )


@pytest.mark.parametrize("status,error", [(302, HTTPFault), (429, RateLimited), (503, HTTPFault)])
async def test_http_errors_never_follow_redirects_or_retry_post(status, error):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.invalid/token"})

    transport = transport_for(handler)
    try:
        with pytest.raises(error):
            await transport.request("POST", "api", "/x/v2/reply/add", data={"csrf": "private"})
        assert len(requests) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "body,error",
    [
        ({"code": -101, "message": "private-cookie"}, LoginExpired),
        ({"code": 12015}, CaptchaRequired),
        ({"code": -509}, RateLimited),
        ({"code": False}, ProtocolFault),
        ({"code": "0"}, ProtocolFault),
        ({"code": 0, "data": {"v_voucher": "private-token"}}, CaptchaRequired),
    ],
)
async def test_business_errors_are_typed_and_sanitized(body, error):
    transport = transport_for(lambda request: httpx.Response(200, json=body))
    try:
        packet = await transport.request("GET", "api", "/x/web-interface/nav")
        with pytest.raises(error) as caught:
            packet.envelope()
        assert "private" not in str(caught.value)
        assert "private" not in repr(packet)
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "body", [b"private-token <html>", b"[]", b'{"code":0}', b'{"code":0,"data":[]}']
)
async def test_malformed_body_is_not_empty_success(body):
    transport = transport_for(lambda request: httpx.Response(200, content=body))
    try:
        packet = await transport.request("GET", "api", "/x/web-interface/nav")
        with pytest.raises(ProtocolFault):
            packet.data()
    finally:
        await transport.close()


async def test_bounded_response_and_fixed_origins():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 101)

    transport = transport_for(handler, max_response_bytes=100)
    try:
        for origin, path in [
            ("attacker", "/x"),
            ("account", "/x"),
            ("https://account.bilibili.com", "/x"),
            ("api", "//attacker.invalid"),
            ("api", "/x?secret"),
        ]:
            with pytest.raises(ProtocolFault):
                await transport.request("GET", origin, path, cookies={"SESSDATA": "private"})
        assert not calls
        with pytest.raises(ProtocolFault):
            await transport.request("GET", "api", "/x")
    finally:
        await transport.close()


async def test_network_fault_no_raw_request_or_retry():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-cookie private-token", request=request)

    transport = transport_for(handler)
    try:
        with pytest.raises(NetworkFault) as caught:
            await transport.request("POST", "api", "/x/v2/reply/add")
        assert len(calls) == 1
        assert "private" not in str(caught.value)
    finally:
        await transport.close()


async def test_explicit_cookie_header_never_leaks_httpx_accumulated_generation():
    headers = []

    def handler(request):
        headers.append(request.headers.get("cookie", ""))
        return httpx.Response(200, json={"code": 0}, headers={"set-cookie": "bili_jct=new; Path=/"})

    transport = transport_for(handler)
    try:
        await transport.request("GET", "api", "/x", cookies={"bili_jct": "old"})
        await transport.request("GET", "api", "/x", cookies={"bili_jct": "old"})
        await transport.request("GET", "api", "/x")
        assert headers == ["bili_jct=old", "bili_jct=old", ""]
    finally:
        await transport.close()


async def test_total_request_deadline_even_when_mock_transport_hangs():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    settings = Settings.model_validate({"platform": {"request_timeout": 0.02}})
    transport = BiliTransport(
        settings, httpx.MockTransport(handler), read_wait=no_wait, write_wait=no_wait
    )
    try:
        with pytest.raises(NetworkFault):
            await transport.request("POST", "api", "/x/v2/reply/add")
        assert entered.is_set()
    finally:
        await transport.close()


async def test_default_http_library_logging_never_exposes_query_tokens(caplog):
    transport = transport_for(lambda request: httpx.Response(200, json={"code": 0}))
    try:
        with caplog.at_level(logging.INFO):
            await transport.request(
                "GET",
                "passport",
                "/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": "private-token"},
            )
        assert "private-token" not in caplog.text
    finally:
        await transport.close()
