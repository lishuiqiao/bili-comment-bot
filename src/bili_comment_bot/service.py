"""Intelligent business entry points. Only application-owned facts select effects."""

import asyncio

from .adapters.bilibili.errors import PlatformError
from .ai.client import AIError
from .ai.prompts import POLICY_VERSION, PROMPT_VERSION
from .config import Settings
from .dispatch import Dispatcher
from .domain import (
    ActionKind,
    ActionStatus,
    Channel,
    Decision,
    FollowState,
    PublishAction,
)
from .policy import discovery_steps
from .storage import Store
from .work import DiscoveryWorkflow, WorkResult, WorkState

SUCCESS = {ActionStatus.SUCCEEDED, ActionStatus.SIMULATED}


class BusinessService:
    def __init__(self, settings: Settings, store: Store, platform, evidence, safety, ai):
        self.settings, self.store, self.platform = settings, store, platform
        self.evidence, self.safety, self.ai = evidence, safety, ai
        self.dispatcher = Dispatcher(settings, store, platform)
        self.discovery_lock = asyncio.Lock()

    async def resume_actions(self, limit: int = 100) -> list[ActionStatus]:
        return [
            await self.dispatcher.execute(action)
            for action in await self.store.pending_actions(limit)
        ]

    async def process_event(self, event_id: str) -> ActionStatus | None:
        return (await self.process_event_result(event_id)).value

    async def process_event_result(self, event_id: str) -> WorkResult:
        if not await self.store.ready_work(event_id) or not await self.store.claim_event(event_id):
            return WorkResult(WorkState.SKIPPED)
        event = await self.store.event(event_id)
        action_id = "reply:" + event.id
        existing = await self.store.action(action_id)
        if existing:
            await self.store.complete_event(event.id, [])
            return WorkResult.action(
                await self.dispatcher.execute(
                    PublishAction.model_validate_json(existing["payload"])
                )
            )
        try:
            if event.channel == Channel.DM:
                follows = await self.platform.sender_follows_bot(event.uid)
                if follows == FollowState.UNKNOWN:
                    raise AIError("follow_state_unknown")
                if follows != FollowState.YES:
                    await self.store.finish_event(event.id, "ignored")
                    return WorkResult(WorkState.SKIPPED)
            limits = self.settings.limits
            quota = limits.dm_per_hour if event.channel == Channel.DM else limits.comment_per_hour
            if not await self.store.quota_available(
                event.uid, event.channel, quota, limits.whitelist
            ):
                await self.store.finish_event(event.id, "ignored")
                return WorkResult(
                    WorkState.SKIPPED
                )  # No quota message; final authority remains Dispatcher.claim_action.
            verdict = await self.safety.check_input(event, None)
            evidence = None
            if verdict.decision == Decision.UNKNOWN:
                raise AIError("input_unknown")
            if verdict.decision == Decision.REJECT:
                purpose, reason = "refuse_request", verdict.reason
            elif event.channel == Channel.DM:
                purpose, reason = "companion", ""
            else:
                evidence = await self.evidence.get_video(event.location.aid)
                if evidence.aid != event.location.aid:
                    raise AIError("evidence_target_mismatch")
                if not evidence.usable:
                    purpose, reason = "insufficient_evidence", evidence.status
                else:
                    source = await self.safety.check_source(evidence)
                    verdict = await self.safety.check_input(event, evidence)
                    if source.decision == Decision.UNKNOWN or verdict.decision == Decision.UNKNOWN:
                        raise AIError("video_safety_unknown")
                    if source.decision == Decision.REJECT or verdict.decision == Decision.REJECT:
                        purpose, reason = "refuse_request", "video_or_request_unsuitable"
                        verdict = verdict.model_copy(update={"decision": Decision.REJECT})
                    else:
                        purpose, reason = "summary", ""
            text = await self.ai.generate(
                purpose,
                message=event.text,
                channel=event.channel.value,
                evidence=evidence,
                reason=reason,
            )
            if not await self.safety.check_output(
                text, purpose=purpose, evidence=evidence, message=event.text
            ):
                raise AIError("output_not_approved")
            action = PublishAction(
                id=action_id,
                kind=ActionKind.REPLY,
                uid=event.uid,
                aid=event.location.aid if event.location else None,
                channel=event.channel,
                location=event.location,
                text=text,
                input_decision=verdict.decision,
                output_safe=True,
                evidence_usable=bool(evidence and evidence.usable),
                refusal=purpose in {"refuse_request", "insufficient_evidence"},
            )
            # Durable reply and completed event are one transaction. Publishing follows it.
            await self.store.complete_event(event.id, [action])
            return WorkResult.action(await self.dispatcher.execute(action))
        except (AIError, PlatformError):
            await self.store.defer_work(event.id, event=True)
            return WorkResult(WorkState.DEFERRED)

    async def discover_video(self, aid: int) -> list[ActionStatus]:
        return (await self.discover_video_result(aid)).value or []

    async def discover_video_result(self, aid: int) -> WorkResult:
        if type(aid) is not int or aid <= 0:
            raise ValueError("video aid must be a positive integer")
        key = f"discovery:{aid}"
        if not self.settings.discovery.invite_uids or not await self.store.ready_work(key):
            return WorkResult(WorkState.SKIPPED, [])
        async with self.discovery_lock:
            if not await self.store.ready_work(key):
                return WorkResult(WorkState.SKIPPED, [])
            try:
                result = await self._discover(aid, key)
                if not await self.store.workflow(key):
                    await self.store.reevaluate_candidate(aid, self.settings.discovery.interval)
                else:
                    steps = discovery_steps(
                        DiscoveryWorkflow.model_validate_json(await self.store.workflow(key)).score
                    )
                    done = (
                        bool(result)
                        and len(result) == len(steps)
                        and all(status in SUCCESS for status in result)
                    )
                    await self.store.discovery_state(key, "done" if done else "paused")
                state = WorkResult.action(result[-1]).state if result else WorkState.SKIPPED
                for status in result:
                    outcome = WorkResult.action(status).state
                    if outcome in {WorkState.FAILED, WorkState.ATTENTION}:
                        state = outcome
                        break
                return WorkResult(state, result)
            except (AIError, PlatformError):
                await self.store.defer_work(key)
                return WorkResult(WorkState.DEFERRED, [])

    async def _discover(self, aid: int, key: str) -> list[ActionStatus]:
        raw = await self.store.workflow(key)
        if raw:
            flow = DiscoveryWorkflow.model_validate_json(raw)
            if (
                flow.aid != aid
                or flow.invite_uids != self.settings.discovery.invite_uids
                or flow.policy_version != POLICY_VERSION
                or flow.prompt_version != PROMPT_VERSION
            ):
                raise AIError("workflow_configuration_changed")
        else:
            evidence = await self.evidence.get_video(aid)
            if evidence.aid != aid or not evidence.usable:
                return []
            if (await self.safety.check_source(evidence)).decision != Decision.ALLOW:
                raise AIError("source_not_approved")
            score, heat = await self.ai.rate(evidence)
            if not discovery_steps(score):
                return []
            flow = DiscoveryWorkflow(
                aid=aid,
                evidence=evidence,
                score=score,
                heat=heat,
                invite_uids=self.settings.discovery.invite_uids,
                policy_version=POLICY_VERSION,
                prompt_version=PROMPT_VERSION,
                model=self.settings.ai.model,
            )
            await self.store.put_workflow(key, flow.model_dump_json())
            flow = DiscoveryWorkflow.model_validate_json(await self.store.workflow(key))
        result, previous = [], None
        source_checked = False
        for kind in discovery_steps(flow.score):
            action_id = key + ":" + kind.value
            stored = await self.store.action(action_id)
            if stored:
                action = PublishAction.model_validate_json(stored["payload"])
            else:
                if not source_checked:
                    if (await self.safety.check_source(flow.evidence)).decision != Decision.ALLOW:
                        raise AIError("source_not_approved")
                    source_checked = True
                text, mentions = "", []
                if kind != ActionKind.LIKE:
                    purpose = "encourage" if kind == ActionKind.ENCOURAGE else "invite"
                    text = await self.ai.generate(
                        purpose, evidence=flow.evidence, score=flow.score.model_dump()
                    )
                    if not await self.safety.check_output(
                        text, purpose=purpose, evidence=flow.evidence
                    ):
                        raise AIError("output_not_approved")
                    if kind == ActionKind.INVITE:
                        mentions = [
                            await self.platform.resolve_identity(uid) for uid in flow.invite_uids
                        ]
                action = PublishAction(
                    id=action_id,
                    kind=kind,
                    aid=aid,
                    text=text,
                    mentions=mentions,
                    dependency=previous,
                    input_decision=Decision.ALLOW,
                    output_safe=True,
                    evidence_usable=True,
                )
                await self.store.put_actions([action])
            status = await self.dispatcher.execute(action)
            result.append(status)
            if status not in SUCCESS:
                break  # Never generate dependent text after failed/uncertain/pending writes.
            previous = action_id
        return result
