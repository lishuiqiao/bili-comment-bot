"""Synthetic protocol fixtures derived from cited API tables; no live account data."""

import time
from contextlib import asynccontextmanager

import httpx
from test_bilibili_auth import credentials, nav, ok
from test_bilibili_transport import no_wait

from bili_comment_bot.adapters.bilibili.auth import AuthManager
from bili_comment_bot.adapters.bilibili.auth_state import CredentialFile
from bili_comment_bot.adapters.bilibili.client import BilibiliClient
from bili_comment_bot.adapters.bilibili.transport import BiliTransport
from bili_comment_bot.config import Settings
from bili_comment_bot.storage import Store


@asynccontextmanager
async def read_client(tmp_path, handler, settings=None):
    settings = settings or Settings()
    requests = []

    async def server(request):
        requests.append(request)
        assert request.method == "GET", "collection/evidence must never write platform"
        if request.url.path.endswith("nav"):
            return ok(nav())
        result = handler(request)
        return await result if hasattr(result, "__await__") else result

    store = await Store(tmp_path / "state.db", settings.namespace, clock=lambda: 10000).open()
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    transport = BiliTransport(
        settings, httpx.MockTransport(server), read_wait=no_wait, write_wait=no_wait
    )
    auth = AuthManager(settings, transport, file)
    client = BilibiliClient(settings, transport, auth, store)
    try:
        await client.verify_identity()
        client.wbi_expires = time.monotonic() + 3600
        yield client, store, requests
    finally:
        await transport.close()
        await store.close()


def at_item(number, *, uid=10, root=0, parent=None, **overrides):
    raw = {
        "id": number,
        "at_time": 9500 + number,
        "user": {"mid": uid},
        "item": {
            "type": "reply",
            "business_id": 1,
            "subject_id": 1,
            "source_id": parent or number * 100,
            "root_id": root,
            "source_content": "请总结这个视频",
            "at_details": [{"mid": 42}],
        },
    }
    raw["item"].update(overrides)
    return raw


def at_response(items, end=True):
    cursor = {"is_end": end}
    if not end:
        cursor.update(id=items[-1]["id"], time=items[-1]["at_time"])
    return ok({"items": items, "cursor": cursor})


def session(uid, seq, stamp=9500_000000):
    return {
        "talker_id": uid,
        "session_type": 1,
        "session_ts": stamp,
        "last_msg": {"msg_seqno": seq},
    }


def dm(seq, uid=10, **overrides):
    raw = {
        "sender_uid": uid,
        "receiver_id": 42,
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "msg_seqno": seq,
        "msg_key": uid * 1000 + seq,
        "content": '{"content":"今天有点累，陪我聊聊"}',
        "timestamp": 9500 + seq,
    }
    raw.update(overrides)
    return raw


def video_details(parts=1):
    return {
        "aid": 1,
        "bvid": "BV1234567890",
        "title": "日常",
        "desc": "简介",
        "pubdate": 9000,
        "stat": dict(view=1000, like=50, coin=10, favorite=20, reply=3, share=5, danmaku=6),
        "pages": [
            {"cid": n, "page": n, "duration": 30, "part": f"第{n}P"} for n in range(1, parts + 1)
        ],
    }


def subtitle(content="早晨散步，晚上看星星。", start=0, end=10):
    return {"body": [{"from": start, "to": end, "content": content}]}
