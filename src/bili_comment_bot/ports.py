from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .domain import (
    FollowState,
    MessageEvent,
    PublishAction,
    PublishReceipt,
    SafetyVerdict,
    VideoEvidence,
)

if TYPE_CHECKING:
    from .adapters.bilibili.collection import AtPage, DmPage, SessionPage
    from .adapters.bilibili.video import (
        AudioTrack,
        CommentSample,
        SubtitleTrack,
        VideoDetails,
        VideoTrack,
    )
    from .ai.transcription import TranscriptResult


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


class TranscriptionPort(Protocol):
    async def transcribe(self, audio: bytes, expected_duration: float) -> TranscriptResult: ...


class CollectionPort(Protocol):
    async def at_page(self, older: tuple[int, int] | None = None) -> AtPage: ...

    async def session_page(self, begin_us: int, end_us: int | None = None) -> SessionPage: ...

    async def dm_page(self, uid: int, begin: int, end: int | None = None) -> DmPage: ...


class VideoPort(Protocol):
    async def details(self, aid: int) -> VideoDetails: ...

    async def comments(self, aid: int) -> CommentSample: ...

    async def subtitle_tracks(self, aid: int, cid: int) -> list[SubtitleTrack]: ...

    async def audio_track(self, aid: int, cid: int) -> AudioTrack | None: ...

    async def video_track(self, aid: int, cid: int) -> VideoTrack | None: ...
