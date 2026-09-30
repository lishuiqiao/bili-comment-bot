"""One guarded side-effect path; no blanket retries of platform writes."""

import asyncio
import unicodedata

from .config import Settings
from .domain import (
    ActionKind,
    ActionStatus,
    Channel,
    Decision,
    DefinitelyNotSent,
    FollowState,
    PublishAction,
)
from .ports import PlatformPort
from .storage import Store


def gate(action: PublishAction, settings: Settings) -> str | None:
    if action.input_decision == Decision.UNKNOWN or not action.output_safe:
        return "safety not approved"
    if action.input_decision == Decision.REJECT and not action.refusal:
        return "only a refusal may answer a rejected request"
    if action.kind != ActionKind.REPLY and action.input_decision != Decision.ALLOW:
        return "discovery input not approved"
    if action.kind == ActionKind.REPLY:
        if action.uid is None or action.channel is None:
            return "reply target missing"
        if action.channel == Channel.COMMENT:
            if not action.location or action.location.aid != action.aid:
                return "original reply location missing"
            if not action.refusal and not action.evidence_usable:
                return "video evidence insufficient"
        elif action.location is not None:
            return "DM cannot carry comment location"
    elif action.aid is None or not action.evidence_usable:
        return "discovery evidence insufficient"
    if action.kind == ActionKind.LIKE:
        if action.text or action.mentions:
            return "like cannot contain text or mentions"
    else:
        if not action.text.strip() or len(action.text) > settings.limits.max_reply_chars:
            return "invalid reply length"
        if "@" in unicodedata.normalize("NFKC", action.text):
            return "model text cannot add mentions"
    if action.mentions:
        if action.kind != ActionKind.INVITE:
            return "mentions are restricted to configured invitations"
        allowed = set(settings.discovery.invite_uids)
        if any(m.uid not in allowed or any(c in m.name for c in "@\r\n") for m in action.mentions):
            return "unconfigured mention identity"
    if action.kind == ActionKind.INVITE and not action.mentions:
        return "invitation requires verified configured identities"
    return None


class Dispatcher:
    def __init__(self, settings: Settings, store: Store, platform: PlatformPort):
        if settings.namespace != store.ns:
            raise ValueError("simulation and live storage namespaces must be isolated")
        self.settings = settings
        self.store = store
        self.platform = platform

    async def execute(self, action: PublishAction) -> ActionStatus:
        # Read the persisted payload, so retries cannot replace a previously approved message.
        await self.store.put_actions([action])
        row = await self.store.action(action.id)
        action = PublishAction.model_validate_json(row["payload"])
        if row["status"] != ActionStatus.PENDING:
            return ActionStatus(row["status"])
        if not await self.store.ready_work("action:" + action.id):
            return ActionStatus.PENDING
        if self.settings.namespace != self.store.ns:
            return ActionStatus(row["status"])
        reason = gate(action, self.settings)
        if not reason and action.kind == ActionKind.REPLY and action.channel == Channel.DM:
            try:
                follows = await self.platform.sender_follows_bot(action.uid)
            except Exception:
                follows = FollowState.UNKNOWN
            if follows != FollowState.YES:
                reason = "sender does not verifiably follow bot"
        # Settings can change while waiting for a read. Retain the pending live action.
        fault = getattr(getattr(self.platform, "transport", None), "auth_fault", None)
        if fault and fault.event.is_set():
            return ActionStatus(row["status"])
        if self.settings.namespace != self.store.ns:
            return ActionStatus((await self.store.action(action.id))["status"])
        if reason:
            return await self.store.block_pending(action.id, reason)
        claim = await self.store.claim_action(
            action.id,
            dm_limit=self.settings.limits.dm_per_hour,
            comment_limit=self.settings.limits.comment_per_hour,
            whitelist=self.settings.limits.whitelist,
        )
        if not claim.claimed:
            return claim.status
        # Claim awaits SQLite: recheck immediately before any platform write.
        if self.settings.namespace != self.store.ns:
            await self.store.finish_action(
                action.id, ActionStatus.FAILED, reason="publishing mode changed before write"
            )
            return ActionStatus.FAILED
        if self.store.ns == "sim":
            await self.store.finish_action(action.id, ActionStatus.SIMULATED)
            return ActionStatus.SIMULATED
        try:
            receipt = await self.platform.publish(action)
        except DefinitelyNotSent:
            status = ActionStatus.FAILED
            await self.store.finish_action(action.id, status, reason="confirmed not sent")
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.finish_action(
                    action.id, ActionStatus.UNCERTAIN, reason="cancelled during write"
                )
            )
            raise
        except Exception:
            status = ActionStatus.UNCERTAIN
            await self.store.finish_action(action.id, status, reason="write outcome unknown")
        else:
            status = ActionStatus.SUCCEEDED
            await self.store.finish_action(action.id, status, remote_id=receipt.remote_id)
        return status
