"""Whole-scope spoken evidence, explicit limitations, validated single-flight cache."""

import asyncio
import hashlib
import json
import math
import time
from urllib.parse import quote

from .adapters.bilibili.collection import mapping, sequence, text
from .adapters.bilibili.download import AUDIO_HOSTS, Downloader
from .adapters.bilibili.errors import ProtocolFault
from .adapters.bilibili.video import VideoDetails
from .ai.client import AIError
from .ai.transcription import TranscriptResult
from .config import Settings
from .domain import PartEvidence, VideoEvidence
from .ports import TranscriptionPort, VideoPort
from .storage import Store

EVIDENCE_VERSION = "spoken-v2"


class TranscriptionUnavailable(AIError):
    def __init__(self):
        super().__init__("transcription_configuration_missing")


def source_id(aid: int, cid: int, kind: str, language: str, model: str = "") -> str:
    model_part = ":model=" + quote(model, safe="") if kind == "transcription" else ""
    return f"bilibili-{kind}:aid={aid}:cid={cid}{model_part}:language=" + quote(language, safe="-_")


def valid_cached_parts(result: VideoEvidence, details: VideoDetails, settings: Settings) -> bool:
    if len(result.parts) != len(details.parts):
        return False
    for saved, actual in zip(result.parts, details.parts, strict=True):
        if (saved.cid, saved.page, saved.duration) != (actual.cid, actual.page, actual.duration):
            return False
        if saved.source_type == "subtitle":
            if saved.language not in settings.evidence.subtitle_languages or saved.model:
                return False
        elif (
            not settings.evidence.transcription_enabled
            or saved.model != settings.evidence.transcription_model
        ):
            return False
        if saved.source_id != source_id(
            details.aid, actual.cid, saved.source_type, saved.language, saved.model
        ):
            return False
    return result.sources == [part.source_id for part in result.parts] and result.languages == [
        part.language for part in result.parts
    ]


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
    def __init__(
        self,
        settings: Settings,
        api: VideoPort,
        downloader: Downloader,
        store: Store,
        transcriber: TranscriptionPort | None = None,
    ):
        self.settings, self.api, self.downloader, self.store = settings, api, downloader, store
        self.transcriber = transcriber
        if settings.evidence.transcription_enabled and transcriber is None:
            raise TranscriptionUnavailable()
        self.tasks: dict[str, asyncio.Task] = {}
        self.metrics = {"cache_hits": 0, "cache_misses": 0}

    async def get_video(self, aid: int) -> VideoEvidence:
        if self.settings.evidence.transcription_enabled and self.transcriber is None:
            raise TranscriptionUnavailable()
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
            "transcription_enabled": config.transcription_enabled,
            "transcription_model": config.transcription_model,
            "transcription_strategy": self.settings.transcription.model_dump(exclude={"api_key"}),
            "download_budget": config.max_download_mb,
            "downloader_byte_limit": self.downloader.max_bytes,
            "part_budget": self.settings.platform.max_pages,
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
                            and valid_cached_parts(result, details, self.settings)
                        )
                    )
                ):
                    self.metrics["cache_hits"] += 1
                    return result
            except ValueError:
                pass  # A malformed cache cannot approve content; reacquire it.
        duration = sum(part.duration for part in details.parts)
        self.metrics["cache_misses"] += 1
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
            or any(part.duration <= 0 for part in details.parts)
            or duration > config.max_video_seconds
            or len(cids) > self.settings.platform.max_pages
        ):
            return VideoEvidence(
                **common,
                status="budget_exceeded",
                limitations=["whole-video duration/part budget exceeded"],
            )
        transcripts, sources, languages, limitations, part_evidence = [], [], [], [], []
        status = "complete"
        remaining_bytes = config.max_download_mb * 1024 * 1024
        transcription_calls = 0
        for part in details.parts:
            tracks = await self.api.subtitle_tracks(details.aid, part.cid)
            rank = {language: index for index, language in enumerate(config.subtitle_languages)}
            candidates = [track for track in tracks if track.language in rank]
            if not candidates:
                if not config.transcription_enabled or tracks:
                    status = "no_subtitle"
                    limitations.append(f"cid={part.cid}: no preferred subtitle track")
                    continue
                if transcription_calls >= self.settings.transcription.max_calls_per_video:
                    status = "transcription_budget_exceeded"
                    limitations.append("whole-video transcription call budget exceeded")
                    break
                track = await self.api.audio_track(details.aid, part.cid)
                if track is None:
                    status = "no_audio"
                    limitations.append(f"cid={part.cid}: no accessible audio")
                    continue
                if track.aid != details.aid or track.cid != part.cid:
                    raise ProtocolFault()
                try:
                    raw = await self.downloader.fetch(
                        track.url,
                        hosts=AUDIO_HOSTS,
                        max_bytes=min(
                            remaining_bytes, self.settings.transcription.max_upload_bytes
                        ),
                    )
                    remaining_bytes -= len(raw)
                except ProtocolFault:
                    status = "audio_download_budget_exceeded"
                    limitations.append(f"cid={part.cid}: invalid target or audio/download budget")
                    break
                transcription_calls += 1
                result = await self.transcriber.transcribe(raw, float(part.duration))
                # Validate replaceable provider outputs as rigorously as the HTTP adapter.
                try:
                    result = TranscriptResult.model_validate(result)
                except (ValueError, TypeError):
                    raise AIError("invalid_transcription_result") from None
                if abs(result.duration - part.duration) > 2:
                    raise AIError("transcription_duration_mismatch")
                transcript = "\n".join(
                    f"[{segment.start:g}-{segment.end:g}] {segment.text.strip()}"
                    for segment in result.segments
                )
                language, kind, model = result.language, "transcription", config.transcription_model
                limitation = (
                    "machine speech transcription may misrecognise; visual content unanalysed"
                )
            else:
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
                language, kind, model = track.language, "subtitle", ""
                limitation = "spoken subtitle content; visual content unanalysed"
            section = f"P{part.page} cid={part.cid} source={kind} language={language}\n{transcript}"
            if len("\n\n".join([*transcripts, section])) > config.max_text_chars:
                status = "text_budget_exceeded"
                limitations.append(f"cid={part.cid}: whole-scope text budget exceeded")
                break
            transcripts.append(section)
            provenance = PartEvidence(
                cid=part.cid,
                page=part.page,
                duration=part.duration,
                source_type=kind,
                source_id=source_id(details.aid, part.cid, kind, language, model),
                language=language,
                model=model,
                acquired_at=time.time(),
                limitation=limitation,
            )
            part_evidence.append(provenance)
            sources.append(provenance.source_id)
            languages.append(language)
            if kind == "transcription":
                limitations.append(f"cid={part.cid}: {limitation}")
        result = VideoEvidence(
            **common,
            transcript="\n\n".join(transcripts),
            sources=sources,
            languages=languages,
            status=status,
            limitations=limitations,
            parts=part_evidence,
            coverage="whole-scope spoken subtitles/transcription; visual content is not analysed",
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
