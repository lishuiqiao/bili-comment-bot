"""Protocol-derived synthetic fixtures, not captured/live Bilibili responses."""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from test_bilibili_auth import IMG_KEY, SUB_KEY, credentials, nav, ok
from test_bilibili_transport import no_wait, transport_for
from test_core import discovery_actions, reply

from bili_comment_bot.adapters.bilibili.auth import AuthManager
from bili_comment_bot.adapters.bilibili.auth_state import CredentialFile
from bili_comment_bot.adapters.bilibili.client import BilibiliClient, sender_relation
from bili_comment_bot.adapters.bilibili.errors import IdentityMismatch
from bili_comment_bot.adapters.bilibili.transport import BiliTransport
from bili_comment_bot.adapters.bilibili.wbi import mixin_key, sign
from bili_comment_bot.config import Settings
from bili_comment_bot.dispatch import Dispatcher
from bili_comment_bot.domain import ActionStatus, FollowState, Mention
from bili_comment_bot.storage import Store


def test_wbi_published_vector_and_canonical_characters():
    # docs/misc/sign/wbi.md public vector; no live keys or account credentials.
    key = mixin_key(IMG_KEY, SUB_KEY)
    assert key == "ea1db124af3c7062474693fa704f4ff8"
    source = {"foo": "114", "bar": "514", "zab": 1919810}
    assert sign(source, key, 1702204169)["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    assert source == {"foo": "114", "bar": "514", "zab": 1919810}
    assert sign({"foo": "a!'()*b"}, key, 1) == sign({"foo": "ab"}, key, 1)
    # Independent digest of the explicitly percent-encoded canonical query in the source notes.
    assert sign({"space": "one one", "中文": "五一四"}, key, 1)["w_rid"] == (
        "414570c2de009b0d0dd8b3e67ea7a314"
    )


@pytest.mark.parametrize(
    "forward,reverse,expected",
    [
        (0, 0, FollowState.NO),
        (2, 0, FollowState.NO),
        (0, 2, FollowState.YES),
        (6, 6, FollowState.YES),
        (0, 1, FollowState.YES),
        (128, 128, FollowState.NO),
    ],
)
def test_relation_direction_is_sender_to_bot(forward, reverse, expected):
    data = {
        "relation": {"mid": 10, "attribute": forward},
        "be_relation": {"mid": 42, "attribute": reverse},
    }
    assert sender_relation(data, 10, 42) == expected


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"mid": 42},
        {"mid": 10, "attribute": 2},
        {"mid": 42, "attribute": True},
        {"mid": 42, "attribute": "2"},
        {"mid": 42, "attribute": 3},
    ],
)
def test_missing_ambiguous_or_wrong_owner_relation_is_unknown(bad):
    assert (
        sender_relation({"relation": {"mid": 10, "attribute": 2}, "be_relation": bad}, 10, 42)
        == FollowState.UNKNOWN
    )


class WriteServer:
    def __init__(self, outcome="success", name="朋友"):
        self.posts = []
        self.outcome = outcome
        self.name = name
        self.nav_calls = 0

    def __call__(self, request):
        if request.url.path.endswith("nav"):
            self.nav_calls += 1
            return ok(nav())
        if request.url.path.endswith("acc/relation"):
            assert request.url.params["w_rid"]
            return ok(
                {
                    "relation": {"mid": 10, "attribute": 0},
                    "be_relation": {"mid": 42, "attribute": 2},
                }
            )
        if request.url.path.endswith("acc/info"):
            return ok({"mid": 123, "name": self.name})
        assert request.method == "POST"
        self.posts.append(request)
        if self.outcome == "reject":
            return httpx.Response(200, json={"code": -111, "message": "private"})
        if self.outcome == "captcha":
            return httpx.Response(200, json={"code": 12015})
        if self.outcome == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if self.outcome == "disconnect":
            raise httpx.RemoteProtocolError("private", request=request)
        if self.outcome == "malformed":
            return ok({})
        if self.outcome == "unknown_code":
            return httpx.Response(200, json={"code": 199999})
        if self.outcome == "http_failure":
            return httpx.Response(503, content=b"private")
        if request.url.path.endswith("archive/like"):
            return ok()
        if request.url.path.endswith("send_msg"):
            return ok({"msg_key": 987})
        return ok({"rpid": 456})


async def platform_setup(tmp_path, server, *, write_wait=no_wait, publishing=None):
    settings = Settings.model_validate(
        {
            "publishing": publishing or {"dry_run": False, "publish_enabled": True},
            "discovery": {"invite_uids": [123]},
            "limits": {"dm_per_hour": 1},
        }
    )
    store = await Store(tmp_path / "state.db", namespace=settings.namespace).open()
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    transport = BiliTransport(
        settings, httpx.MockTransport(server), read_wait=no_wait, write_wait=write_wait
    )
    auth = AuthManager(settings, transport, file, identity_guard=store.bind_account)
    client = BilibiliClient(settings, transport, auth, store)
    await client.verify_identity()
    return settings, store, transport, client


async def test_dm_sender_identity_content_encoding_and_cached_wbi(tmp_path):
    server = WriteServer()
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        assert (
            await Dispatcher(settings, store, client).execute(reply(text='你好，"朋友"。'))
            == ActionStatus.SUCCEEDED
        )
        fields = parse_qs(server.posts[0].content.decode())
        assert fields["msg[sender_uid]"] == ["42"]
        assert fields["msg[receiver_id]"] == ["10"]
        assert json.loads(fields["msg[content]"][0]) == {"content": '你好，"朋友"。'}
        assert fields["csrf"] == fields["csrf_token"] == ["old-csrf"]
        assert server.posts[0].url.params["w_sender_uid"] == "42"
        assert server.nav_calls == 1
    finally:
        await transport.close()
        await store.close()


async def test_comment_original_root_and_parent_and_invite_mapping(tmp_path):
    from bili_comment_bot.domain import Channel

    server = WriteServer()
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        dispatcher = Dispatcher(settings, store, client)
        assert await dispatcher.execute(reply(channel=Channel.COMMENT)) == ActionStatus.SUCCEEDED
        fields = parse_qs(server.posts[0].content.decode())
        assert (fields["oid"], fields["type"], fields["root"], fields["parent"]) == (
            ["1"],
            ["1"],
            ["100"],
            ["200"],
        )
        invite = discovery_actions()[-1].model_copy(update={"dependency": None})
        assert await client.resolve_identity(123) == Mention(uid=123, name="朋友")
        assert await dispatcher.execute(invite) == ActionStatus.SUCCEEDED
        fields = parse_qs(server.posts[-1].content.decode())
        assert fields["root"] == fields["parent"] == ["0"]
        assert json.loads(fields["at_name_to_mid"][0]) == {"朋友": 123}
        assert fields["message"][0].startswith("@朋友 ")
    finally:
        await transport.close()
        await store.close()


@pytest.mark.parametrize(
    "name", ["另一个名字", "@额外用户", "＠全角", "朋友\n陌生人", "朋友\u202e"]
)
async def test_stale_or_unsafe_identity_never_posts(name, tmp_path):
    server = WriteServer(name=name)
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        invite = discovery_actions()[-1].model_copy(update={"dependency": None})
        assert await Dispatcher(settings, store, client).execute(invite) == ActionStatus.FAILED
        assert not server.posts
    finally:
        await transport.close()
        await store.close()


async def test_final_mention_length_and_dm_byte_limits(tmp_path):
    server = WriteServer()
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        settings.limits.max_reply_chars = 50
        invite = discovery_actions()[-1].model_copy(update={"dependency": None, "text": "长" * 49})
        assert await Dispatcher(settings, store, client).execute(invite) == ActionStatus.FAILED
        settings.limits.max_reply_chars = 800
        assert (
            await Dispatcher(settings, store, client).execute(reply(text="长" * 700))
            == ActionStatus.FAILED
        )
        assert not server.posts
    finally:
        await transport.close()
        await store.close()


async def test_like_explicit_desired_state_ack_has_no_invented_remote_id(tmp_path):
    server = WriteServer()
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        item = discovery_actions()[0]
        assert await Dispatcher(settings, store, client).execute(item) == ActionStatus.SUCCEEDED
        fields = parse_qs(server.posts[0].content.decode())
        assert fields["aid"] == ["1"] and fields["like"] == ["1"]
        assert (await store.action(item.id))["remote_id"] is None
    finally:
        await transport.close()
        await store.close()


@pytest.mark.parametrize(
    "outcome,status,quota",
    [
        ("success", ActionStatus.SUCCEEDED, "sent"),
        ("reject", ActionStatus.FAILED, "released"),
        ("captcha", ActionStatus.FAILED, "released"),
        *[
            (error, ActionStatus.UNCERTAIN, "uncertain")
            for error in ("timeout", "disconnect", "malformed", "unknown_code", "http_failure")
        ],
    ],
)
async def test_write_results_drive_durable_state_without_retries(outcome, status, quota, tmp_path):
    server = WriteServer(outcome)
    settings, store, transport, client = await platform_setup(tmp_path, server)
    try:
        dispatcher = Dispatcher(settings, store, client)
        assert await dispatcher.execute(reply()) == status
        assert await dispatcher.execute(reply()) == status
        assert len(server.posts) == 1
        async with store.transaction() as db:
            row = await (
                await db.execute("SELECT state FROM quota WHERE action_id='reply:1'")
            ).fetchone()
        assert row[0] == quota
    finally:
        await transport.close()
        await store.close()


async def test_publish_switch_off_inside_adapter_rate_wait_never_posts(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def write_wait():
        entered.set()
        await release.wait()

    server = WriteServer()
    settings, store, transport, client = await platform_setup(
        tmp_path, server, write_wait=write_wait
    )
    task = asyncio.create_task(Dispatcher(settings, store, client).execute(reply()))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            settings.publishing.publish_enabled = False
            release.set()
            assert await task == ActionStatus.FAILED
        assert not server.posts
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await transport.close()
        await store.close()


async def test_simulated_real_adapter_never_enters_post_path(tmp_path):
    server = WriteServer()
    settings, store, transport, client = await platform_setup(
        tmp_path, server, publishing={"dry_run": True, "publish_enabled": True}
    )
    try:
        assert await Dispatcher(settings, store, client).execute(reply()) == ActionStatus.SIMULATED
        assert not server.posts
    finally:
        await transport.close()
        await store.close()


async def test_account_binding_conflict_survives_restart_and_cross_namespace(tmp_path):
    path = tmp_path / "state.db"
    store = await Store(path).open()
    await store.bind_account(42)
    await store.close()
    reopened = await Store(path, namespace="sim").open()
    try:
        await reopened.bind_account(42)
        with pytest.raises(ValueError):
            await reopened.bind_account(43)
    finally:
        await reopened.close()


async def test_new_login_cannot_replace_credentials_of_bound_account(tmp_path):
    from bili_comment_bot.adapters.bilibili.auth import QRChallenge

    def handler(request):
        if request.url.path.endswith("poll"):
            headers = [
                ("set-cookie", f"{name}={value}; Path=/")
                for name, value in {
                    "SESSDATA": "another-session",
                    "bili_jct": "another-csrf",
                    "DedeUserID": "43",
                }.items()
            ]
            return ok({"code": 0, "refresh_token": "another-token"}, headers)
        return ok(nav(uid=43))

    store = await Store(tmp_path / "state.db").open()
    await store.bind_account(42)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    before = file.path.read_bytes()
    transport = transport_for(handler)
    try:
        auth = AuthManager(Settings(), transport, file, identity_guard=store.bind_account)
        with pytest.raises(IdentityMismatch):
            await auth.poll_qr(
                QRChallenge(key="private-key", url="https://passport.bilibili.com/x")
            )
        assert file.path.read_bytes() == before
        assert auth.state.uid == 42
    finally:
        await transport.close()
        await store.close()


async def test_relationship_network_failure_returns_unknown(tmp_path):
    def handler(request):
        if request.url.path.endswith("nav"):
            return ok(nav())
        raise httpx.ConnectError("private-token", request=request)

    transport = transport_for(handler)
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    store = await Store(tmp_path / "state.db").open()
    try:
        settings = Settings()
        auth = AuthManager(settings, transport, file)
        client = BilibiliClient(settings, transport, auth, store)
        assert await client.sender_follows_bot(10) == FollowState.UNKNOWN
    finally:
        await transport.close()
        await store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": {"bot_uid": True}},
        {"platform": {"bot_uid": "42"}},
        {"limits": {"whitelist": [True]}},
        {"discovery": {"invite_uids": ["123"]}},
    ],
)
def test_config_recipient_ids_are_strict_integers(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate(payload)
