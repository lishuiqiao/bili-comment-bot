"""Whole-scope spoken evidence, explicit limitations, validated single-flight cache."""

import asyncio
import hashlib
import json
import math
import time

from .adapters.bilibili.collection import mapping, sequence, text
from .adapters.bilibili.download import Downloader
from .adapters.bilibili.errors import ProtocolFault
from .adapters.bilibili.video import VideoDetails
from .config import Settings
from .domain import VideoEvidence
from .ports import VideoPort
from .storage import Store

EVIDENCE_VERSION = "subtitle-v1"


class TranscriptionUnavailable(RuntimeError):
    pass


def subtitle_text(body: dict, duration: int) -> str:
    segments = sequence(body.get("body"))
    if not segments:
        raise ProtocolFault()
    lines, last_start = [], -1.0
    for value in segments:
        raw = mapping(value)
        start, end = raw.get("from"), raw.get("to")
        if (
            type(start) not in {int, float}
            or type(end) not in {int, float}
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or start < last_start
            or end > duration + 2
        ):
            raise ProtocolFault()
        content = text(raw.get("content")).strip()
        if not content:
            raise ProtocolFault()
        lines.append(f"[{start:g}-{end:g}] {content}")
        last_start = start
    return "\n".join(lines)


class EvidenceService:
    def __init__(self, settings: Settings, api: VideoPort, downloader: Downloader, store: Store):
        self.settings, self.api, self.downloader, self.store = settings, api, downloader, store
        self.tasks: dict[str, asyncio.Task] = {}

    async def get_video(self, aid: int) -> VideoEvidence:
        details = await self.api.details(aid)
        config = self.settings.evidence
        cids = [part.cid for part in details.parts]
        identity = {
            "version": EVIDENCE_VERSION,
            "aid": aid,
            "cids": cids,
            "languages": config.subtitle_languages,
            "text_budget": config.max_text_chars,
            "duration_budget": config.max_video_seconds,
        }
        key = "content:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if key not in self.tasks:
            task = asyncio.create_task(self._content(key, details))
            self.tasks[key] = task

            def release(done):
                if self.tasks.get(key) is done:
                    self.tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Consume failure when all awaiting callers were cancelled.

            task.add_done_callback(release)
        content = await asyncio.shield(self.tasks[key])
        sample = await self.api.comments(aid)
        return content.model_copy(
            update={
                "stats": details.stats,
                "snapshot_at": details.acquired_at,
                "comments": sample.comments,
                "comment_sample": sample.model_dump(),
                "title": details.title,
                "description": details.description,
            }
        )

    async def _content(self, key: str, details: VideoDetails) -> VideoEvidence:
        config = self.settings.evidence
        cids = [part.cid for part in details.parts]
        try:
            cached = await self.store.cache_get(key)
        except (ValueError, UnicodeError):
            cached = None
        if cached:
            try:
                result = VideoEvidence.model_validate(cached)
                if (
                    result.aid == details.aid
                    and result.bvid == details.bvid
                    and result.scope_cids == cids
                    and len(result.transcript) <= config.max_text_chars
                    and result.duration == sum(part.duration for part in details.parts)
                    and result.content_acquired_at > 0
                    and (
                        not result.complete
                        or (
                            result.status == "complete"
                            and result.usable
                            and len(result.languages) == len(cids)
                            and all(
                                language in config.subtitle_languages
                                for language in result.languages
                            )
                            and result.sources
                            == [
                                f"bilibili-subtitle:aid={details.aid}:cid={cid}:language={language}"
                                for cid, language in zip(cids, result.languages, strict=True)
                            ]
                        )
                    )
                ):
                    return result
            except ValueError:
                pass  # A malformed cache cannot approve content; reacquire it.
        duration = sum(part.duration for part in details.parts)
        common = dict(
            aid=details.aid,
            bvid=details.bvid,
            title=details.title,
            description=details.description,
            duration=duration,
            published_at=details.published_at,
            scope_cids=cids,
            content_acquired_at=time.time(),
        )
        if (
            duration <= 0
            or duration > config.max_video_seconds
            or len(cids) > self.settings.platform.max_pages
        ):
            return VideoEvidence(
                **common,
                status="budget_exceeded",
                limitations=["whole-video duration/part budget exceeded"],
            )
        transcripts, sources, languages, limitations = [], [], [], []
        status = "complete"
        remaining_bytes = config.max_download_mb * 1024 * 1024
        for part in details.parts:
            tracks = await self.api.subtitle_tracks(details.aid, part.cid)
            rank = {language: index for index, language in enumerate(config.subtitle_languages)}
            candidates = [track for track in tracks if track.language in rank]
            if not candidates:
                if config.transcription_enabled:
                    raise TranscriptionUnavailable("无字幕转写将在 AI 阶段接入，当前能力尚未实现")
                status = "no_subtitle"
                limitations.append(f"cid={part.cid}: no preferred subtitle track")
                continue
            track = min(candidates, key=lambda item: rank[item.language])
            try:
                raw = await self.downloader.fetch(track.url, max_bytes=remaining_bytes)
                remaining_bytes -= len(raw)
            except ProtocolFault:
                status = "invalid_or_oversized_subtitle"
                limitations.append(
                    f"cid={part.cid}: invalid URL or byte budget; acquisition stopped"
                )
                break
            try:
                body = mapping(json.loads(raw))
                transcript = subtitle_text(body, part.duration)
            except (ProtocolFault, ValueError, UnicodeError):
                status = "invalid_or_oversized_subtitle"
                limitations.append(f"cid={part.cid}: invalid structure/time")
                continue
            section = f"P{part.page} cid={part.cid} language={track.language}\n{transcript}"
            if len("\n\n".join([*transcripts, section])) > config.max_text_chars:
                status = "text_budget_exceeded"
                limitations.append(f"cid={part.cid}: whole-scope text budget exceeded")
                break
            transcripts.append(section)
            sources.append(
                f"bilibili-subtitle:aid={details.aid}:cid={part.cid}:language={track.language}"
            )
            languages.append(track.language)
        result = VideoEvidence(
            **common,
            transcript="\n\n".join(transcripts),
            sources=sources,
            languages=languages,
            status=status,
            limitations=limitations,
            complete=status == "complete" and len(sources) == len(cids),
        )
        # Cache content only; dynamic statistics/comments are refreshed separately.
        await self.store.cache_put(
            key, result.model_dump(), config.cache_ttl if result.complete else 60
        )
        return result

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
