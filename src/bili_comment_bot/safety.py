"""Deterministic gates plus independent model checks; unknown never approves."""

import re
import unicodedata

from .ai.client import AIError
from .ai.contracts import InputAssessment, OutputAssessment
from .ai.prompts import system_prompt
from .config import Settings
from .domain import Channel, Decision, MessageEvent, SafetyVerdict, VideoEvidence


def normalize(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold()
        if not unicodedata.category(char).startswith("C")
    )


def rule_rejection(value: str, settings: Settings, channel: Channel | None = None) -> str | None:
    normalized = normalize(value)
    compact = re.sub(r"\s+", "", normalized)
    if any(
        normalize(word).strip() and normalize(word).replace(" ", "") in compact
        for word in settings.unsafe_words
    ):
        return "harmful"
    if re.search(
        r"(忽略|绕过|覆盖|忘记).{0,12}(规则|指令|系统|安全)|"
        r"(ignore|override|bypass).{0,30}(instructions|system|safety)|"
        r"(泄露|展示|输出).{0,10}(系统提示|密钥|cookie)|"
        r"(system|developer)\s*[:：]|<\|(?:system|im_start)\|>",
        normalized,
    ):
        return "injection"
    if re.search(
        r"(教|帮|给|替|请|如何).{0,20}(制造炸弹|诈骗|偷取密码|窃取隐私|杀人方法|未成年色情)",
        compact,
    ):
        return "harmful"
    if channel == Channel.DM and re.search(
        r"(帮|给|替|请|能否|howto).{0,30}(写代码|编程|debug|修复代码|调试|写程序|写脚本|sql|"
        r"投资建议|诊断|法律意见|做作业)|(?:write|debug|implement).{0,20}(code|script|program)",
        compact,
    ):
        return "out_of_scope"
    return None


def evidence_data(evidence: VideoEvidence | None) -> dict | None:
    if evidence is None:
        return None
    return {
        "title": evidence.title,
        "description": evidence.description,
        "transcript": evidence.transcript,
        "sources": evidence.sources,
        "scope_cids": evidence.scope_cids,
        "coverage": evidence.coverage,
        "limitations": evidence.limitations,
        "comments": evidence.comments,
        "comment_sample": evidence.comment_sample,
    }


class SafetyService:
    def __init__(self, settings: Settings, client):
        self.settings, self.client = settings, client

    async def _assess(self, purpose, data) -> SafetyVerdict:
        try:
            result = await self.client.complete(
                system_prompt(purpose, self.settings.persona), data, InputAssessment
            )
        except AIError:
            return SafetyVerdict(decision=Decision.UNKNOWN, reason="check_failed")
        consistent = (
            (result.decision == Decision.ALLOW and result.category == "allowed")
            or (
                result.decision == Decision.REJECT and result.category not in {"allowed", "unknown"}
            )
            or (result.decision == Decision.UNKNOWN and result.category == "unknown")
        )
        return SafetyVerdict(
            decision=result.decision if consistent else Decision.UNKNOWN,
            reason=result.category if consistent else "inconsistent_assessment",
        )

    async def check_input(
        self, event: MessageEvent, evidence: VideoEvidence | None
    ) -> SafetyVerdict:
        reason = (
            "out_of_scope"
            if len(event.text) > self.settings.limits.max_message_chars
            else rule_rejection(event.text, self.settings, event.channel)
        )
        if reason:
            return SafetyVerdict(decision=Decision.REJECT, reason=reason)
        return await self._assess(
            "input_safety",
            {
                "channel": event.channel.value,
                "message": event.text,
                "video_evidence": evidence_data(evidence),
                "stage": "video_relevance" if evidence else "precheck",
            },
        )

    async def check_source(self, evidence: VideoEvidence) -> SafetyVerdict:
        fields = [evidence.title, evidence.description, evidence.transcript, *evidence.comments]
        for value in fields:
            reason = rule_rejection(value, self.settings)
            if reason:
                return SafetyVerdict(decision=Decision.REJECT, reason=reason)
        return await self._assess("source_safety", {"video_evidence": evidence_data(evidence)})

    async def check_output(
        self,
        value: str,
        *,
        purpose: str = "companion",
        evidence: VideoEvidence | None = None,
        message: str = "",
    ) -> bool:
        if (
            not value.strip()
            or len(value) > self.settings.limits.max_reply_chars
            or "@" in normalize(value)
            or rule_rejection(value, self.settings)
        ):
            return False
        try:
            result = await self.client.complete(
                system_prompt("output_safety", self.settings.persona),
                {
                    "purpose": purpose,
                    "text": value,
                    "user_message": message,
                    "video_evidence": evidence_data(evidence),
                },
                OutputAssessment,
            )
        except AIError:
            return False
        return result.safe and result.category == "allowed"
