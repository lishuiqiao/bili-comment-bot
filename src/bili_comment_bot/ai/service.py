from ..config import Settings
from ..domain import VideoEvidence, VideoScore
from ..safety import evidence_data
from ..scoring import heat_report
from .client import AIError
from .contracts import ContentRating, GeneratedText
from .prompts import PURPOSES, system_prompt


def validate_citations(citations: list[str], evidence: VideoEvidence | None, *, all_sources=False):
    if evidence is None:
        if citations:
            raise AIError("unexpected_citation")
        return
    if not evidence.usable or not citations or not set(citations) <= set(evidence.sources):
        raise AIError("unbound_citation")
    if all_sources and set(citations) != set(evidence.sources):
        raise AIError("incomplete_source_coverage")


class AIService:
    def __init__(self, settings: Settings, client):
        self.settings, self.client = settings, client

    async def generate(
        self,
        purpose: str,
        *,
        message: str = "",
        channel: str = "",
        evidence: VideoEvidence | None = None,
        reason: str = "",
        score=None,
    ) -> str:
        if purpose not in PURPOSES:
            raise ValueError("unknown generation purpose")
        if purpose in {"summary", "encourage", "invite"} and (
            evidence is None or not evidence.usable
        ):
            raise AIError("insufficient_evidence")
        safe_refusal = purpose in {"refuse_request", "insufficient_evidence"}
        result = await self.client.complete(
            system_prompt(purpose, self.settings.persona),
            {
                "message": "" if safe_refusal else message,
                "channel": channel,
                "reason": reason,
                "video_evidence": None if safe_refusal else evidence_data(evidence),
                "score": score,
            },
            GeneratedText,
        )
        validate_citations(
            result.citations, None if safe_refusal else evidence, all_sources=purpose == "summary"
        )
        output = result.text.strip()
        if purpose == "summary":
            scope = ",".join(f"P{number}" for number in range(1, len(evidence.scope_cids) + 1))
            output += f"\n（依据全视频 {scope} 字幕；未分析画面。）"
        return output

    async def rate(self, evidence: VideoEvidence) -> tuple[VideoScore, dict]:
        if not evidence.usable or len(evidence.comments) < 3:
            raise AIError("insufficient_scoring_evidence")
        try:
            report = heat_report(evidence)
        except ValueError:
            raise AIError("invalid_measurements") from None
        result = await self.client.complete(
            system_prompt("rating", self.settings.persona),
            {"video_evidence": evidence_data(evidence)},
            ContentRating,
        )
        validate_citations(result.citations, evidence)
        score = VideoScore(
            heat=report["score"],
            recommendation=result.recommendation,
            absurdity=result.absurdity,
            reasons=[
                "objective " + report["version"],
                result.recommendation_reason,
                result.absurdity_reason,
            ],
        )
        return score, report
