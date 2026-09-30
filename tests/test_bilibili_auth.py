"""Source-derived synthetic envelopes and explicit fault injection; no live response claims."""

import asyncio
import os
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from test_bilibili_transport import transport_for

from bili_comment_bot.adapters.bilibili.auth import AuthManager, QRChallenge, QRStatus, correspond_path
from bili_comment_bot.adapters.bilibili.auth_state import CredentialFile, Credentials, RefreshPhase
from bili_comment_bot.adapters.bilibili.errors import (
    CaptchaRequired,
    IdentityMismatch,
    LoginExpired,
    NetworkFault,
    ReauthenticationRequired,
)
from bili_comment_bot.config import Settings

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def credentials(csrf="old-csrf", token="old-token", phase=RefreshPhase.STABLE):
    return Credentials(
        uid=42,
        cookies={"SESSDATA": "private-session", "bili_jct": csrf, "DedeUserID": "42"},
        refresh_token=token,
        old_token="old-token"
        if phase in {RefreshPhase.CONFIRM_PENDING, RefreshPhase.CONFIRM_STARTED}
        else "",
        phase=phase,
    )


def nav(uid=42, logged_in=True):
    return {
        "isLogin": logged_in,
        "mid": uid,
        "wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
        },
    }


def ok(data=None, headers=None):
    return httpx.Response(200, json={"code": 0, "data": data}, headers=headers)


def cookie_headers(csrf="new-csrf"):
    return [
        ("set-cookie", f"{key}={value}; Path=/; Domain=.bilibili.com")
        for key, value in {
            "SESSDATA": "new-session",
            "bili_jct": csrf,
            "DedeUserID": "42",
        }.items()
    ]


@pytest.mark.parametrize(
    "code,expected",
    [(86101, QRStatus.WAITING_SCAN), (86090, QRStatus.WAITING_CONFIRM), (86038, QRStatus.EXPIRED)],
)
async def test_qr_pending_and_expired_states(code, expected, tmp_path):
    transport = transport_for(lambda request: ok({"code": code}))
    manager = AuthManager(Settings(), transport, CredentialFile(tmp_path / "auth.json"))
    try:
        challenge = QRChallenge(key="private-key", url="https://passport.bilibili.com/qrcode/x")
        assert await manager.poll_qr(challenge) == expected
        assert manager.state is None
        assert "private-key" not in repr(challenge)
    finally:
        await transport.close()


@pytest.mark.parametrize("via_url", [False, True])
async def test_qr_success_verified_and_saved_without_leaking_secrets(via_url, tmp_path):
    def handler(request):
        if request.url.path.endswith("generate"):
            return ok(
                {"qrcode_key": "private-key", "url": "https://passport.bilibili.com/qrcode/x"}
            )
        if request.url.path.endswith("poll"):
            data = {"code": 0, "refresh_token": "private-refresh"}
            if via_url:
                data["url"] = (
                    "https://passport.bilibili.com/login?SESSDATA=new-session&bili_jct=new-csrf&DedeUserID=42"
                )
            return ok(data, None if via_url else cookie_headers())
        assert "new-csrf" in request.headers["cookie"]
        return ok(nav())

    transport = transport_for(handler)
    file = CredentialFile(tmp_path / "auth.json")
    manager = AuthManager(Settings(), transport, file)
    try:
        challenge = await manager.generate_qr()
        image = tmp_path / "login.png"
        challenge.write_image(image)
        assert image.stat().st_mode & 0o777 == 0o600
        assert await manager.poll_qr(challenge) == QRStatus.SUCCEEDED
        assert file.load().uid == 42
        assert file.path.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert "new-session" not in repr(manager.state)
        assert "private-refresh" not in manager.state.model_dump_json()
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "nav_uid,configured_uid,exception",
    [(43, 0, IdentityMismatch), (42, 43, IdentityMismatch), (42, 0, LoginExpired)],
)
async def test_login_rejects_wrong_identity_or_invalid_session(
    nav_uid, configured_uid, exception, tmp_path
):
    def handler(request):
        if request.url.path.endswith("poll"):
            return ok({"code": 0, "refresh_token": "private-refresh"}, cookie_headers())
        return ok(nav(nav_uid, exception is not LoginExpired))

    settings = Settings.model_validate({"platform": {"bot_uid": configured_uid}})
    transport = transport_for(handler)
    file = CredentialFile(tmp_path / "auth.json")
    try:
        manager = AuthManager(settings, transport, file)
        with pytest.raises(exception):
            await manager.poll_qr(QRChallenge(key="private", url="https://passport.bilibili.com/x"))
        assert not file.path.exists()
    finally:
        await transport.close()


async def test_qr_captcha_is_not_retried(tmp_path):
    transport = transport_for(
        lambda request: httpx.Response(200, json={"code": -352, "message": "private"})
    )
    try:
        manager = AuthManager(Settings(), transport, CredentialFile(tmp_path / "auth.json"))
        with pytest.raises(CaptchaRequired):
            await manager.generate_qr()
    finally:
        await transport.close()


def test_atomic_credential_replace_failure_keeps_old_file(tmp_path, monkeypatch):
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    before = file.path.read_bytes()

    def fail_replace(*args):
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        file.save(credentials("new-csrf", "new-token"))
    assert file.path.read_bytes() == before
    assert not list(tmp_path.glob(".auth-*"))
    file.path.chmod(0o644)
    with pytest.raises(PermissionError):
        file.load()


class RefreshServer:
    def __init__(self, *, fail_refresh=False, fail_confirm=False):
        self.calls = []
        self.refreshes = 0
        self.confirms = 0
        self.fail_refresh = fail_refresh
        self.fail_confirm = fail_confirm

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("nav"):
            return ok(nav())
        if path.endswith("info"):
            return ok({"refresh": self.refreshes == 0, "timestamp": 1702204169000})
        if "/correspond/" in path:
            assert len(path.rsplit("/", 1)[1]) == 256
            return httpx.Response(200, content=b'<html><div id="1-name">refresh-csrf</div></html>')
        if path.endswith("cookie/refresh"):
            self.refreshes += 1
            assert parse_qs(request.content.decode())["csrf"] == ["old-csrf"]
            assert parse_qs(request.content.decode())["refresh_token"] == ["old-token"]
            assert "bili_jct=old-csrf" in request.headers["cookie"]
            if self.fail_refresh:
                raise httpx.ReadTimeout("private-token", request=request)
            return ok({"refresh_token": "new-token", "status": 0}, cookie_headers())
        assert path.endswith("confirm/refresh")
        self.confirms += 1
        fields = parse_qs(request.content.decode())
        assert fields["csrf"] == ["new-csrf"]
        assert fields["refresh_token"] == ["old-token"]
        assert "bili_jct=new-csrf" in request.headers["cookie"]
        if self.fail_confirm:
            raise httpx.ReadTimeout("private-token", request=request)
        return ok()


async def test_refresh_complete_and_concurrent_requests_rotate_once(tmp_path):
    server = RefreshServer()
    transport = transport_for(server)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    try:
        manager = AuthManager(Settings(), transport, file)
        assert sorted(await asyncio.gather(manager.refresh(), manager.refresh())) == [False, True]
        assert (server.refreshes, server.confirms) == (1, 1)
        saved = file.load()
        assert saved.csrf == "new-csrf"
        assert saved.refresh_token.get_secret_value() == "new-token"
        assert saved.old_token == SecretStr("")
        assert saved.phase == RefreshPhase.STABLE
    finally:
        await transport.close()


@pytest.mark.parametrize("phase", [RefreshPhase.REFRESH_STARTED, RefreshPhase.CONFIRM_STARTED])
async def test_unknown_post_result_on_restart_is_frozen_not_retried(phase, tmp_path):
    calls = []
    transport = transport_for(lambda request: calls.append(request))
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials(phase=phase))
    try:
        manager = AuthManager(Settings(), transport, file)
        with pytest.raises(ReauthenticationRequired):
            await manager.refresh()
        with pytest.raises(ReauthenticationRequired):
            async with manager.credentials():
                pass
        assert not calls
    finally:
        await transport.close()


async def test_new_credentials_saved_before_confirm_resume_without_rotating_again(tmp_path):
    server = RefreshServer()
    transport = transport_for(server)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials("new-csrf", "new-token", RefreshPhase.CONFIRM_PENDING))
    try:
        manager = AuthManager(Settings(), transport, file)
        assert await manager.refresh()
        assert (server.refreshes, server.confirms) == (0, 1)
        assert file.load().phase == RefreshPhase.STABLE
    finally:
        await transport.close()


@pytest.mark.parametrize("fail_confirm", [False, True])
async def test_lost_post_response_records_phase_before_request(tmp_path, fail_confirm):
    server = RefreshServer(fail_refresh=not fail_confirm, fail_confirm=fail_confirm)
    transport = transport_for(server)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    try:
        manager = AuthManager(Settings(), transport, file)
        with pytest.raises(NetworkFault):
            await manager.refresh()
        expected = RefreshPhase.CONFIRM_STARTED if fail_confirm else RefreshPhase.REFRESH_STARTED
        assert file.load().phase == expected
        restart = AuthManager(Settings(), transport, file)
        with pytest.raises(ReauthenticationRequired):
            await restart.refresh()
        assert server.refreshes == 1
        assert server.confirms == int(fail_confirm)
    finally:
        await transport.close()


@pytest.mark.parametrize("fail_phase", list(RefreshPhase))
async def test_interruption_at_each_persistence_boundary(tmp_path, monkeypatch, fail_phase):
    server = RefreshServer()
    transport = transport_for(server)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    original = file.save

    def fail_save(state):
        if state.phase == fail_phase:
            raise OSError("injected write failure")
        original(state)

    monkeypatch.setattr(file, "save", fail_save)
    try:
        manager = AuthManager(Settings(), transport, file)
        with pytest.raises(OSError):
            await manager.refresh()
        persisted = file.load()
        if fail_phase == RefreshPhase.REFRESH_STARTED:
            assert persisted.phase == RefreshPhase.STABLE
            assert server.refreshes == 0
        elif fail_phase == RefreshPhase.CONFIRM_PENDING:
            assert persisted.phase == RefreshPhase.REFRESH_STARTED
            assert (server.refreshes, server.confirms) == (1, 0)
        elif fail_phase == RefreshPhase.CONFIRM_STARTED:
            assert persisted.phase == RefreshPhase.CONFIRM_PENDING
            assert (server.refreshes, server.confirms) == (1, 0)
        else:
            assert persisted.phase == RefreshPhase.CONFIRM_STARTED
            assert (server.refreshes, server.confirms) == (1, 1)
    finally:
        await transport.close()


def test_correspond_path_uses_sha256_oaep_and_randomized_ciphertext():
    # Public key is 1024-bit; OAEP randomization means there is no fixed ciphertext vector.
    first, second = correspond_path(1702204169000), correspond_path(1702204169000)
    assert len(bytes.fromhex(first)) == 128
    assert first != second


def test_correspond_plaintext_and_padding_against_independent_private_key(monkeypatch):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from bili_comment_bot.adapters.bilibili import auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth.serialization, "load_pem_public_key", lambda pem: key.public_key())
    encrypted = bytes.fromhex(correspond_path(1702204169000))
    assert (
        key.decrypt(
            encrypted,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        == b"refresh_1702204169000"
    )


async def test_platform_operation_waits_for_refresh_and_uses_one_generation(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    server = RefreshServer()

    async def handler(request):
        if request.url.path.endswith("cookie/refresh"):
            entered.set()
            await release.wait()
        return server(request)

    transport = transport_for(handler)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    manager = AuthManager(Settings(), transport, file)
    observed = []

    async def platform_operation():
        async with manager.credentials() as state:
            observed.append((state.cookie_values()["bili_jct"], state.csrf))

    refresh_task = asyncio.create_task(manager.refresh())
    operation_task = None
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            operation_task = asyncio.create_task(platform_operation())
            assert manager.lock.locked()
            release.set()
            await asyncio.gather(refresh_task, operation_task)
        assert observed == [("new-csrf", "new-csrf")]
    finally:
        release.set()
        await asyncio.gather(
            refresh_task, *([operation_task] if operation_task else []), return_exceptions=True
        )
        await transport.close()
