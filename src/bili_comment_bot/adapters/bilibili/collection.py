"""Read-only protocol adapters. Invalid pages never masquerade as empty pages."""

import json
from dataclasses import dataclass

from ...domain import Channel, MessageEvent, ReplyLocation
from .client import BilibiliClient
from .errors import IdentityMismatch, ProtocolFault


def integer(value, minimum=0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ProtocolFault()
    return value


def mapping(value) -> dict:
    if not isinstance(value, dict):
        raise ProtocolFault()
    return value


def sequence(value, *, nullable=False) -> list:
    if nullable and value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolFault()
    return value


def nullable_sequence(data: dict, key: str) -> list:
    if key not in data:
        raise ProtocolFault()
    return sequence(data[key], nullable=True)


def text(value) -> str:
    if not isinstance(value, str):
        raise ProtocolFault()
    return value


def more_flag(value) -> bool:
    if type(value) is not int or value not in {0, 1}:
        raise ProtocolFault()
    return bool(value)


@dataclass(frozen=True)
class AtRecord:
    position: tuple[int, int]
    event: MessageEvent | None
    ignored: str = ""


@dataclass(frozen=True)
class AtPage:
    records: list[AtRecord]
    older: tuple[int, int] | None
    is_end: bool


@dataclass(frozen=True)
class SessionRecord:
    uid: int
    updated_us: int
    latest_seq: int
    supported: bool


@dataclass(frozen=True)
class SessionPage:
    records: list[SessionRecord]
    has_more: bool


@dataclass(frozen=True)
class DmRecord:
    seq: int
    event: MessageEvent | None
    ignored: str = ""


@dataclass(frozen=True)
class DmPage:
    records: list[DmRecord]
    has_more: bool


class Reader:
    def __init__(self, client: BilibiliClient):
        self.client = client

    async def get(self, origin: str, path: str, params: dict, *, signed=False) -> dict:
        async with self.client.auth.credentials() as state:
            if self.client.verified_uid != state.uid:
                raise IdentityMismatch()
            if signed:
                params = await self.client._signed(state, params)
            return (
                await self.client.transport.request(
                    "GET", origin, path, params=params, cookies=state.cookie_values()
                )
            ).data()


class CollectionAPI(Reader):
    async def at_page(self, older: tuple[int, int] | None = None) -> AtPage:
        params = {"mobi_app": "web"}
        if older:
            params.update(at_time=older[0], id=older[1])
        data = await self.get("api", "/x/msgfeed/at", params)
        cursor = mapping(data.get("cursor"))
        if type(cursor.get("is_end")) is not bool:
            raise ProtocolFault()
        records = [self._at(mapping(item)) for item in sequence(data.get("items"))]
        next_position = None
        if not cursor["is_end"]:
            next_position = (
                integer(cursor.get("at_time", cursor.get("time")), 1),
                integer(cursor.get("id"), 1),
            )
            if not records:
                raise ProtocolFault()
        return AtPage(records, next_position, cursor["is_end"])

    def _at(self, raw: dict) -> AtRecord:
        position = (integer(raw.get("at_time"), 1), integer(raw.get("id"), 1))
        item, user = mapping(raw.get("item")), mapping(raw.get("user"))
        uid = integer(user.get("mid"), 1)
        if item.get("type") != "reply" or item.get("business_id") != 1:
            return AtRecord(position, None, "non_video")
        if uid == self.client.verified_uid:
            return AtRecord(position, None, "self")
        # The authenticated @ feed is addressed to the verified account. If an explicit
        # mention list is present and nonempty, verify it agrees; never match raw @ text.
        details = sequence(item.get("at_details", []))
        if details and self.client.verified_uid not in [
            integer(mapping(detail).get("mid"), 1) for detail in details
        ]:
            return AtRecord(position, None, "other_target")
        aid, parent = integer(item.get("subject_id"), 1), integer(item.get("source_id"), 1)
        root = integer(item.get("root_id")) or parent
        event = MessageEvent(
            id=f"comment:{aid}:{parent}",
            uid=uid,
            channel=Channel.COMMENT,
            text=text(item.get("source_content")),
            timestamp=position[0],
            location=ReplyLocation(aid=aid, root=root, parent=parent),
        )
        return AtRecord(position, event)

    async def session_page(self, begin_us: int, end_us: int | None = None) -> SessionPage:
        params = {
            "session_type": 4,
            "group_fold": 0,
            "unfollow_fold": 0,
            "sort_rule": 2,
            "size": 100,
            "mobi_app": "web",
            "begin_ts": begin_us,
        }
        if end_us is not None:
            params["end_ts"] = end_us
        data = await self.get("message", "/session_svr/v1/session_svr/get_sessions", params)
        return self._sessions(data)

    async def new_sessions(self, begin_us: int) -> SessionPage:
        data = await self.get(
            "message",
            "/session_svr/v1/session_svr/new_sessions",
            {
                "begin_ts": begin_us,
                "mobi_app": "web",
            },
        )
        # Caller must still drain paginated discovery when has_more is true.
        return self._sessions(data)

    def _sessions(self, data: dict) -> SessionPage:
        records = []
        for value in nullable_sequence(data, "session_list"):
            raw = mapping(value)
            supported = integer(raw.get("session_type"), 1) == 1
            uid, stamp = integer(raw.get("talker_id"), 1), integer(raw.get("session_ts"), 1)
            last = raw.get("last_msg")
            seq = integer(mapping(last).get("msg_seqno"), 1) if last is not None else 0
            records.append(SessionRecord(uid, stamp, seq, supported))
        return SessionPage(records, more_flag(data.get("has_more")))

    async def dm_page(self, uid: int, begin: int, end: int | None = None) -> DmPage:
        params = {"talker_id": uid, "session_type": 1, "size": 100, "begin_seqno": begin}
        if end is not None:
            params["end_seqno"] = end
        data = await self.get(
            "message", "/svr_sync/v1/svr_sync/fetch_session_msgs", params, signed=True
        )
        records = []
        for value in nullable_sequence(data, "messages"):
            raw = mapping(value)
            seq = integer(raw.get("msg_seqno"), 1)
            sender, receiver = integer(raw.get("sender_uid"), 1), integer(raw.get("receiver_id"), 1)
            kind, status = integer(raw.get("msg_type"), 1), integer(raw.get("msg_status"))
            receiver_type = integer(raw.get("receiver_type"), 1)
            event, reason = None, "unsupported"
            if sender == self.client.verified_uid:
                reason = "self"
            elif sender != uid or receiver != self.client.verified_uid or receiver_type != 1:
                raise ProtocolFault()
            elif status != 0:
                reason = "withdrawn"
            elif kind == 1:
                try:
                    body = mapping(json.loads(text(raw.get("content"))))
                except (ValueError, UnicodeError):
                    raise ProtocolFault() from None
                event = MessageEvent(
                    id=f"dm:{uid}:{integer(raw.get('msg_key'), 1)}",
                    uid=uid,
                    channel=Channel.DM,
                    text=text(body.get("content")),
                    timestamp=integer(raw.get("timestamp"), 1),
                )
                reason = ""
            records.append(DmRecord(seq, event, reason))
        more = more_flag(data.get("has_more"))
        if more and not records:
            raise ProtocolFault()
        return DmPage(records, more)
