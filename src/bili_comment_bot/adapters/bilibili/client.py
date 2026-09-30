"""Verified identity, relationship direction and guarded platform side effects."""

import json
import time
import unicodedata
import uuid

from ...config import Settings
from ...dispatch import gate
from ...domain import (
    ActionKind,
    Channel,
    DefinitelyNotSent,
    FollowState,
    Mention,
    PublishAction,
    PublishReceipt,
    UncertainWrite,
)
from ...storage import Store
from .auth import AuthManager
from .auth_state import Credentials
from .errors import (
    CaptchaRequired,
    IdentityMismatch,
    LoginExpired,
    PlatformError,
    ProtocolFault,
    ReauthenticationRequired,
)
from .transport import BiliTransport
from .wbi import key_from_nav, sign

# Known documented rejections only. Unknown business codes may describe an ambiguous write.
REJECTED_WRITES = {
    -101,
    -102,
    -105,
    -111,
    -352,
    -400,
    -404,
    -509,
    12002,
    12003,
    12006,
    12009,
    12015,
    12016,
    12025,
    12035,
    12045,
    12052,
}


def safe_identity_name(name: str) -> bool:
    normalized = unicodedata.normalize("NFKC", name)
    return (
        bool(name.strip())
        and len(name) <= 50
        and not any(char in normalized for char in "@\r\n")
        and not any(unicodedata.category(char).startswith("C") for char in name)
    )


def sender_relation(data: dict, sender_uid: int, bot_uid: int) -> FollowState:
    """be_relation targets the logged-in bot; relation targets the queried sender.

    The community table's direction labels conflict with running bot source. We validate
    both owners and use the latter's observed convention; live asymmetric acceptance remains.
    """
    relation, reverse = data.get("relation"), data.get("be_relation")
    if not isinstance(relation, dict) or not isinstance(reverse, dict):
        return FollowState.UNKNOWN
    if type(relation.get("mid")) is not int or type(reverse.get("mid")) is not int:
        return FollowState.UNKNOWN
    if relation["mid"] != sender_uid or reverse["mid"] != bot_uid:
        return FollowState.UNKNOWN
    values = (relation.get("attribute"), reverse.get("attribute"))
    if any(type(value) is not int or value not in {0, 1, 2, 6, 128} for value in values):
        return FollowState.UNKNOWN
    return FollowState.YES if reverse["attribute"] in {1, 2, 6} else FollowState.NO


class BilibiliClient:
    def __init__(
        self, settings: Settings, transport: BiliTransport, auth: AuthManager, store: Store
    ):
        self.settings = settings
        self.transport = transport
        self.auth = auth
        self.store = store
        self.verified_uid: int | None = None
        self.wbi_key: str | None = None
        self.wbi_expires = 0.0

    async def verify_identity(self) -> int:
        data = await self.auth.verify()
        await self.store.bind_account(data["mid"])
        self.verified_uid = data["mid"]
        self.wbi_key = key_from_nav(data)
        self.wbi_expires = time.monotonic() + 3600
        return self.verified_uid

    async def _signed(self, state: Credentials, params: dict) -> dict:
        if self.wbi_key is None or time.monotonic() >= self.wbi_expires:
            nav = await self.auth._verify(state)
            self.wbi_key = key_from_nav(nav)
            self.wbi_expires = time.monotonic() + 3600
        return sign(params, self.wbi_key, int(time.time()))

    async def sender_follows_bot(self, uid: int) -> FollowState:
        try:
            async with self.auth.credentials() as state:
                data = (
                    await self.transport.request(
                        "GET",
                        "api",
                        "/x/space/wbi/acc/relation",
                        cookies=state.cookie_values(),
                        params=await self._signed(state, {"mid": uid}),
                    )
                ).data()
                return sender_relation(data, uid, state.uid)
        except PlatformError:
            return FollowState.UNKNOWN

    async def _resolve_identity(self, state: Credentials, uid: int) -> Mention:
        data = (
            await self.transport.request(
                "GET",
                "api",
                "/x/space/wbi/acc/info",
                cookies=state.cookie_values(),
                params=await self._signed(state, {"mid": uid}),
            )
        ).data()
        if type(data.get("mid")) is not int or data["mid"] != uid:
            raise ProtocolFault()
        name = data.get("name")
        if not isinstance(name, str) or not safe_identity_name(name):
            raise ProtocolFault()
        return Mention(uid=uid, name=name)

    async def resolve_identity(self, uid: int) -> Mention:
        if uid not in self.settings.discovery.invite_uids:
            raise ValueError("identity must come from configured invite UIDs")
        async with self.auth.credentials() as state:
            return await self._resolve_identity(state, uid)

    def _publish_guard(self):
        if self.settings.namespace != "live" or self.store.ns != "live":
            raise DefinitelyNotSent("publishing is disabled")

    async def publish(self, action: PublishAction) -> PublishReceipt:
        self._publish_guard()
        if gate(action, self.settings):
            raise DefinitelyNotSent("action did not pass publish gate")
        attempted = False

        def before_send():
            nonlocal attempted
            self._publish_guard()
            if gate(action, self.settings):
                raise DefinitelyNotSent("action approval changed before write")
            attempted = True

        try:
            async with self.auth.credentials() as state:
                if self.verified_uid != state.uid:
                    raise IdentityMismatch()
                if self.settings.platform.bot_uid not in {0, state.uid}:
                    raise IdentityMismatch()
                self._publish_guard()
                if action.kind == ActionKind.LIKE:
                    origin, path = "api", "/x/web-interface/archive/like"
                    data = {"aid": action.aid, "like": 1, "csrf": state.csrf}
                    params = None
                elif action.kind == ActionKind.REPLY and action.channel == Channel.DM:
                    # Check immediately before waiting for HTTP, even if Dispatcher checked earlier.
                    relation = (
                        await self.transport.request(
                            "GET",
                            "api",
                            "/x/space/wbi/acc/relation",
                            params=await self._signed(state, {"mid": action.uid}),
                            cookies=state.cookie_values(),
                        )
                    ).data()
                    if sender_relation(relation, action.uid, state.uid) != FollowState.YES:
                        raise DefinitelyNotSent("sender does not verifiably follow bot")
                    content = json.dumps({"content": action.text}, ensure_ascii=False)
                    if len(content.encode()) > 2000:
                        raise DefinitelyNotSent("DM exceeds platform byte limit")
                    device = str(uuid.uuid4())
                    origin, path = "message", "/web_im/v1/web_im/send_msg"
                    data = {
                        "msg[sender_uid]": state.uid,
                        "msg[receiver_id]": action.uid,
                        "msg[receiver_type]": 1,
                        "msg[msg_type]": 1,
                        "msg[msg_status]": 0,
                        "msg[content]": content,
                        "msg[dev_id]": device,
                        "msg[timestamp]": int(time.time()),
                        "msg[new_face_version]": 0,
                        "csrf": state.csrf,
                        "csrf_token": state.csrf,
                        "mobi_app": "web",
                    }
                    params = await self._signed(
                        state,
                        {
                            "w_sender_uid": state.uid,
                            "w_receiver_id": action.uid,
                            "w_dev_id": device,
                        },
                    )
                else:
                    names = {}
                    for mention in action.mentions:
                        identity = await self._resolve_identity(state, mention.uid)
                        if identity.name != mention.name or identity.name in names:
                            raise DefinitelyNotSent("mention identity changed or conflicts")
                        names[identity.name] = identity.uid
                    message = " ".join(f"@{name}" for name in names)
                    message = (message + " " + action.text).strip() if names else action.text
                    if len(message) > min(1000, self.settings.limits.max_reply_chars):
                        raise DefinitelyNotSent("final comment exceeds length limit")
                    origin, path = "api", "/x/v2/reply/add"
                    data = {
                        "oid": action.aid,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "plat": 1,
                        "message": message,
                        "csrf": state.csrf,
                        "at_name_to_mid": json.dumps(names, ensure_ascii=False),
                    }
                    if action.location:
                        data.update(
                            root=action.location.root or action.location.parent,
                            parent=action.location.parent,
                        )
                    params = None
                packet = await self.transport.request(
                    "POST",
                    origin,
                    path,
                    cookies=state.cookie_values(),
                    data=data,
                    params=params,
                    publish_guard=before_send,
                )
                envelope = packet.envelope()
                if action.kind == ActionKind.LIKE:
                    return PublishReceipt()
                result = envelope.get("data")
                if not isinstance(result, dict):
                    raise ProtocolFault()
                if result.get("need_captcha"):
                    if self.transport.auth_fault:
                        self.transport.auth_fault.notify(CaptchaRequired())
                    raise ProtocolFault()
                field = "msg_key" if origin == "message" else "rpid"
                remote_id = result.get(field)
                if isinstance(remote_id, bool) or not isinstance(remote_id, (str, int)):
                    raise ProtocolFault()
                if not str(remote_id).isdigit() or int(remote_id) <= 0:
                    raise ProtocolFault()
                return PublishReceipt(remote_id=str(remote_id))
        except DefinitelyNotSent:
            raise
        except (LoginExpired, ReauthenticationRequired, IdentityMismatch):
            raise DefinitelyNotSent("login identity unavailable") from None
        except PlatformError as error:
            if not attempted or error.code in REJECTED_WRITES:
                raise DefinitelyNotSent("platform rejected or request not issued") from None
            raise UncertainWrite("platform write outcome unknown") from None
