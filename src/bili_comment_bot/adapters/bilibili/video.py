"""Video facts and bounded samples, separate from AI judgements."""

import html
import math
import re
import time

from pydantic import Field

from ...domain import Contract
from .collection import Reader, integer, mapping, nullable_sequence, sequence, text
from .download import AUDIO_HOSTS, checked_url
from .errors import ProtocolFault


class VideoPart(Contract):
    cid: int = Field(gt=0, strict=True)
    page: int = Field(gt=0, strict=True)
    duration: int = Field(ge=0, strict=True)
    title: str


class VideoDetails(Contract):
    aid: int = Field(gt=0, strict=True)
    bvid: str
    title: str
    description: str
    published_at: int = Field(ge=0, strict=True)
    stats: dict[str, int]
    parts: list[VideoPart]
    acquired_at: float


class Candidate(Contract):
    aid: int = Field(gt=0, strict=True)
    bvid: str
    title: str
    keywords: list[str]
    acquired_at: float


class CommentSample(Contract):
    comments: list[str]
    acquired_at: float
    sort: str = "hot"
    truncated: bool
    total_reported: int
    limitation: str = "hot-comment sample; not representative of all viewers"


class SubtitleTrack(Contract):
    language: str
    url: str = Field(repr=False)


class AudioTrack(Contract):
    aid: int = Field(gt=0, strict=True)
    cid: int = Field(gt=0, strict=True)
    duration: float = Field(gt=0, allow_inf_nan=False, strict=True)
    url: str = Field(repr=False)
    codec: str
    format: str = "m4a"
    binding: str = "verified requested aid/cid and playback duration"


class VideoAPI(Reader):
    async def audio_track(self, aid: int, cid: int) -> AudioTrack | None:
        # playurl does not always echo IDs. Verify membership before the signed request,
        # compare playback duration, and validate any optional echoed IDs when present.
        details = await self.details(aid)
        part = next((part for part in details.parts if part.cid == cid), None)
        if part is None or part.duration <= 0:
            raise ProtocolFault()
        data = await self.get(
            "api",
            "/x/player/wbi/playurl",
            {"avid": aid, "cid": cid, "fnval": 16, "fnver": 0, "qn": 16},
            signed=True,
        )
        for key, value in (("aid", aid), ("cid", cid)):
            if key in data and integer(data[key], 1) != value:
                raise ProtocolFault()
        duration = integer(data.get("timelength"), 1) / 1000
        if abs(duration - part.duration) > 2:
            raise ProtocolFault()
        dash = mapping(data.get("dash"))
        if "duration" in dash:
            raw = dash["duration"]
            if type(raw) not in {int, float} or not math.isfinite(raw) or abs(raw - duration) > 2:
                raise ProtocolFault()
        audios = nullable_sequence(dash, "audio")
        candidates = []
        for entry in audios:
            entry = mapping(entry)
            codec = text(entry.get("codecs"))
            bandwidth = integer(entry.get("bandwidth"), 1)
            if not codec.startswith("mp4a."):
                continue  # Unsupported codecs do not get uploaded under a false format.
            if "base_url" in entry and "baseUrl" in entry and entry["base_url"] != entry["baseUrl"]:
                raise ProtocolFault()
            primary = text(entry.get("base_url", entry.get("baseUrl")))
            backups = sequence(entry.get("backup_url", entry.get("backupUrl", [])), nullable=True)
            for raw_url in [primary, *backups]:
                try:
                    url = checked_url(text(raw_url), AUDIO_HOSTS)
                except ProtocolFault:
                    continue
                candidates.append((bandwidth, codec, url))
                break  # Choose one address; failed transfers are never retried implicitly.
        if not candidates:
            if audios:
                raise ProtocolFault()  # Present but unsupported/untrusted is not "no audio".
            return None
        _, codec, url = min(candidates, key=lambda item: item[0])
        return AudioTrack(aid=aid, cid=cid, duration=duration, codec=codec, url=url)

    async def details(self, aid: int) -> VideoDetails:
        data = await self.get("api", "/x/web-interface/view", {"aid": aid})
        if integer(data.get("aid"), 1) != aid:
            raise ProtocolFault()
        bvid = text(data.get("bvid"))
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            raise ProtocolFault()
        parts = []
        for raw in sequence(data.get("pages")):
            raw = mapping(raw)
            parts.append(
                VideoPart(
                    cid=integer(raw.get("cid"), 1),
                    page=integer(raw.get("page"), 1),
                    duration=integer(raw.get("duration")),
                    title=text(raw.get("part")),
                )
            )
        if (
            not parts
            or len({part.cid for part in parts}) != len(parts)
            or [part.page for part in parts] != list(range(1, len(parts) + 1))
        ):
            raise ProtocolFault()
        stats = mapping(data.get("stat"))
        counters = {
            key: integer(stats.get(key))
            for key in ("view", "like", "coin", "favorite", "reply", "share", "danmaku")
        }
        return VideoDetails(
            aid=aid,
            bvid=bvid,
            title=text(data.get("title")),
            description=text(data.get("desc")),
            published_at=integer(data.get("pubdate")),
            stats=counters,
            parts=parts,
            acquired_at=time.time(),
        )

    async def search_page(self, keyword: str, page: int) -> list[Candidate]:
        data = await self.get(
            "api",
            "/x/web-interface/wbi/search/type",
            {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
            },
            signed=True,
        )
        result = []
        for value in nullable_sequence(data, "result"):
            raw = mapping(value)
            if raw.get("type") != "video":
                raise ProtocolFault()
            if not re.fullmatch(r"BV[0-9A-Za-z]{10}", text(raw.get("bvid"))):
                raise ProtocolFault()
            result.append(
                Candidate(
                    aid=integer(raw.get("aid"), 1),
                    bvid=text(raw.get("bvid")),
                    title=html.unescape(re.sub(r"<[^>]*>", "", text(raw.get("title")))),
                    keywords=[keyword],
                    acquired_at=time.time(),
                )
            )
        return result

    async def search_candidates(self) -> list[Candidate]:
        settings = self.client.settings.discovery
        found = {}
        for keyword in settings.keywords:
            for page in range(1, settings.pages_per_keyword + 1):
                results = await self.search_page(keyword, page)
                for candidate in results:
                    if candidate.aid in found:
                        previous = found[candidate.aid]
                        found[candidate.aid] = previous.model_copy(
                            update={"keywords": sorted(set(previous.keywords + candidate.keywords))}
                        )
                    elif len(found) < settings.videos_per_cycle:
                        found[candidate.aid] = candidate
                if not results:
                    break
        return list(found.values())

    async def comments(self, aid: int) -> CommentSample:
        limit = self.client.settings.evidence.comment_sample_size
        comments, total, truncated = [], 0, False
        for page in range(1, min(self.client.settings.platform.max_pages, (limit + 19) // 20) + 1):
            data = await self.get(
                "api",
                "/x/v2/reply",
                {
                    "oid": aid,
                    "type": 1,
                    "sort": 2,
                    "pn": page,
                    "ps": 20,
                },
            )
            total = integer(mapping(data.get("page")).get("count"))
            replies = nullable_sequence(data, "replies")
            for raw in replies:
                message = text(mapping(mapping(raw).get("content")).get("message"))
                if len(message) > 2000:
                    truncated = True
                if len(comments) < limit:
                    comments.append(message[:2000])
                else:
                    truncated = True
            if len(comments) >= limit or not replies:
                break
        return CommentSample(
            comments=comments,
            acquired_at=time.time(),
            total_reported=total,
            truncated=truncated or len(comments) < total,
        )

    async def subtitle_tracks(self, aid: int, cid: int) -> list[SubtitleTrack]:
        data = await self.get("api", "/x/player/wbi/v2", {"aid": aid, "cid": cid}, signed=True)
        if integer(data.get("aid"), 1) != aid or integer(data.get("cid"), 1) != cid:
            raise ProtocolFault()
        subtitles = sequence(mapping(data.get("subtitle")).get("subtitles"))
        return [
            SubtitleTrack(
                language=text(mapping(raw).get("lan")), url=text(mapping(raw).get("subtitle_url"))
            )
            for raw in subtitles
        ]
