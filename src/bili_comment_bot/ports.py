from typing import Protocol

from .domain import (
    FollowState,
    MessageEvent,
    PublishAction,
    PublishReceipt,
    SafetyVerdict,
    VideoEvidence,
)


class PlatformPort(Protocol):
    async def sender_follows_bot(self, uid: int) -> FollowState: ...

    async def publish(self, action: PublishAction) -> PublishReceipt: ...


class SafetyPort(Protocol):
    async def check_input(
        self, event: MessageEvent, evidence: VideoEvidence | None
    ) -> SafetyVerdict: ...

    async def check_output(self, text: str) -> bool: ...


class EvidencePort(Protocol):
    async def get_video(self, aid: int) -> VideoEvidence: ...
