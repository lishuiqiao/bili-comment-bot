import asyncio
import math

import httpx
import pytest
from bili_read_fixtures import read_client, subtitle, video_details
from test_bilibili_auth import ok

from bili_comment_bot.adapters.bilibili.download import Downloader, checked_url
from bili_comment_bot.adapters.bilibili.errors import HTTPFault, NetworkFault, ProtocolFault
from bili_comment_bot.adapters.bilibili.video import VideoAPI
from bili_comment_bot.config import Settings
from bili_comment_bot.evidence import EvidenceService, TranscriptionUnavailable, subtitle_text


class VideoServer:
    def __init__(self, parts=1, no_subtitle=None):
        self.parts, self.no_subtitle, self.calls = parts, no_subtitle, []
        self.views = 1000

    def __call__(self, request):
        self.calls.append(request)
        path, params = request.url.path, request.url.params
        if path.endswith("/view"):
            data = video_details(self.parts)
            data["stat"]["view"] = self.views
            return ok(data)
        if path.endswith("/v2"):
            assert params["w_rid"]
            cid = int(params["cid"])
            return ok(
                {
                    "aid": 1,
                    "cid": cid,
                    "subtitle": {
                        "subtitles": []
                        if cid == self.no_subtitle
                        else [
                            {
                                "lan": "en",
                                "subtitle_url": (
                                    f"//aisubtitle.hdslb.com/{cid}-en.json?auth_key=private"
                                ),
                            },
                            {
                                "lan": "zh-CN",
                                "subtitle_url": (
                                    f"//aisubtitle.hdslb.com/{cid}.json?auth_key=private"
                                ),
                            },
                        ]
                    },
                }
            )
        if path.endswith("/reply"):
            return ok({"page": {"count": 100}, "replies": [{"content": {"message": "挺有趣"}}]})
        raise AssertionError("unexpected endpoint")


@pytest.mark.parametrize(
    "url",
    [
        "http://aisubtitle.hdslb.com/a",
        "https://aisubtitle.hdslb.com.evil.invalid/a",
        "https://evil-aisubtitle.hdslb.com/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://localhost/a",
        "https://user@aisubtitle.hdslb.com/a",
        "https://aisubtitle.hdslb.com:444/a",
        "https://aisubtitle.hdslb.com/a#token",
        "//evil.invalid/a",
        "https://aisubtitle.hdslb.com\\@evil.invalid/a",
    ],
)
async def test_downloader_rejects_untrusted_targets_before_any_request(url):
    calls = []
    downloader = Downloader(1, 100, httpx.MockTransport(lambda r: calls.append(r)))
    try:
        with pytest.raises(ProtocolFault):
            await downloader.fetch(url)
        assert not calls
    finally:
        await downloader.close()


async def test_downloader_https_normalization_no_cookie_redirect_and_actual_size(caplog):
    calls = []

    def server(r):
        calls.append(r)
        assert not r.headers["cookie"]
        if r.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://evil.invalid/a"})
        return httpx.Response(200, content=b"x" * 101)

    downloader = Downloader(1, 100, httpx.MockTransport(server))
    try:
        assert checked_url("//aisubtitle.hdslb.com/a") == "https://aisubtitle.hdslb.com/a"
        with pytest.raises(HTTPFault):
            await downloader.fetch("//aisubtitle.hdslb.com/redirect?auth_key=private")
        assert len(calls) == 1
        with pytest.raises(ProtocolFault):
            await downloader.fetch("//aisubtitle.hdslb.com/a")
        assert len(calls) == 2
        assert "private" not in caplog.text
    finally:
        await downloader.close()


async def test_downloader_total_timeout_cancels_stalled_stream():
    entered = asyncio.Event()

    async def handler(r):
        entered.set()
        await asyncio.Event().wait()

    downloader = Downloader(0.02, 100, httpx.MockTransport(handler))
    try:
        with pytest.raises(NetworkFault):
            await downloader.fetch("https://aisubtitle.hdslb.com/a")
        assert entered.is_set()
    finally:
        await downloader.close()


@pytest.mark.parametrize(
    "start,end", [(-1, 1), (0, 0), (2, 1), (0, 33), (math.nan, 1), (0, math.inf), (True, 1)]
)
def test_subtitle_invalid_times_never_become_complete(start, end):
    with pytest.raises(ProtocolFault):
        subtitle_text(subtitle(start=start, end=end), 30)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"body": []},
        {"body": [{"from": 0, "to": 1, "content": ""}]},
        {"body": [{"from": 2, "to": 3, "content": "a"}, {"from": 1, "to": 2, "content": "b"}]},
    ],
)
def test_subtitle_empty_invalid_or_reversed_segments(body):
    with pytest.raises(ProtocolFault):
        subtitle_text(body, 30)


async def test_multi_p_coverage_language_cache_fresh_metrics_and_sampling(tmp_path):
    server, downloads = VideoServer(2), []

    def cdn(r):
        downloads.append(r)
        assert "cookie" in r.headers and not r.headers["cookie"]
        return httpx.Response(200, json=subtitle())

    async with read_client(tmp_path, server) as (client, store, requests):
        downloader = Downloader(1, 100000, httpx.MockTransport(cdn))
        service = EvidenceService(client.settings, VideoAPI(client), downloader, store)
        try:
            evidence = await service.get_video(1)
            assert evidence.usable and evidence.scope_cids == [1, 2]
            assert evidence.languages == ["zh-CN", "zh-CN"]
            assert "P1" in evidence.transcript and "P2" in evidence.transcript
            assert "visual" in evidence.coverage
            assert evidence.comment_sample["truncated"] and evidence.comment_sample["sort"] == "hot"
            assert evidence.comment_sample["total_reported"] == 100
            assert "private" not in evidence.model_dump_json()
            acquired = evidence.content_acquired_at
            server.views = 9999
            newer = await service.get_video(1)
            assert len(downloads) == 2 and newer.stats["view"] == 9999
            assert newer.content_acquired_at == acquired
            assert all(request.method == "GET" for request in requests)
        finally:
            await service.close()
            await downloader.close()


@pytest.mark.parametrize(
    "condition,status",
    [
        ("missing_part", "no_subtitle"),
        ("empty", "invalid_or_oversized_subtitle"),
        ("bad_time", "invalid_or_oversized_subtitle"),
        ("text", "text_budget_exceeded"),
        ("duration", "budget_exceeded"),
        ("oversized", "invalid_or_oversized_subtitle"),
    ],
)
async def test_partial_empty_bad_budget_content_is_insufficient(tmp_path, condition, status):
    server = VideoServer(2, no_subtitle=2 if condition == "missing_part" else None)
    settings = Settings.model_validate(
        {
            "evidence": {
                "max_text_chars": 1000,
                "max_video_seconds": 30 if condition == "duration" else 1800,
            }
        }
    )

    def cdn(r):
        body = (
            {"body": []}
            if condition == "empty"
            else subtitle(
                content="长" * 1100 if condition == "text" else "早晨散步",
                end=90 if condition == "bad_time" else 10,
            )
        )
        return httpx.Response(200, json=body)

    async with read_client(tmp_path, server, settings) as (client, store, _):
        downloader = Downloader(
            1, 10 if condition == "oversized" else 100000, httpx.MockTransport(cdn)
        )
        service = EvidenceService(settings, VideoAPI(client), downloader, store)
        try:
            evidence = await service.get_video(1)
            assert not evidence.usable and not evidence.complete and evidence.status == status
            assert evidence.limitations
        finally:
            await service.close()
            await downloader.close()


async def test_enabled_transcription_is_explicitly_unavailable_in_this_milestone(tmp_path):
    settings = Settings.model_validate({"evidence": {"transcription_enabled": True}})
    async with read_client(tmp_path, VideoServer(no_subtitle=1), settings) as (client, store, _):
        downloader = Downloader(
            1, 1000, httpx.MockTransport(lambda r: pytest.fail("should not download"))
        )
        service = EvidenceService(settings, VideoAPI(client), downloader, store)
        try:
            with pytest.raises(TranscriptionUnavailable):
                await service.get_video(1)
        finally:
            await service.close()
            await downloader.close()


async def test_cache_corruption_expiry_and_scope_change_reacquire(tmp_path):
    server, calls = VideoServer(), []

    def cdn(r):
        calls.append(r)
        return httpx.Response(200, json=subtitle())

    async with read_client(tmp_path, server) as (client, store, _):
        downloader = Downloader(1, 100000, httpx.MockTransport(cdn))
        service = EvidenceService(client.settings, VideoAPI(client), downloader, store)
        try:
            assert (await service.get_video(1)).usable
            await store.db.execute("UPDATE cache SET payload='{}'")
            assert (await service.get_video(1)).usable and len(calls) == 2
            await store.db.execute("UPDATE cache SET payload='not JSON'")
            assert (await service.get_video(1)).usable and len(calls) == 3
            await store.db.execute("UPDATE cache SET expires=0")
            assert (await service.get_video(1)).usable and len(calls) == 4
            server.parts = 2
            assert (await service.get_video(1)).scope_cids == [1, 2] and len(calls) == 6
            client.settings.evidence.subtitle_languages = ["en"]
            changed = await service.get_video(1)
            assert changed.usable and changed.languages == ["en", "en"] and len(calls) == 8
        finally:
            await service.close()
            await downloader.close()


async def test_http_read_to_events_search_candidate_and_video_evidence_has_zero_writes(tmp_path):
    from bili_read_fixtures import at_item, at_response

    from bili_comment_bot.adapters.bilibili.collection import CollectionAPI
    from bili_comment_bot.collectors import Collectors

    server = VideoServer()

    def combined(r):
        if r.url.path.endswith("/at"):
            return at_response([at_item(2)])
        if r.url.path.endswith("/search/type"):
            return ok(
                {"result": [{"type": "video", "aid": 1, "bvid": "BV1234567890", "title": "日常"}]}
            )
        return server(r)

    settings = Settings.model_validate(
        {"discovery": {"keywords": ["日常"]}, "platform": {"history_lookback_seconds": 1000}}
    )
    async with read_client(tmp_path, combined, settings) as (client, store, requests):
        downloader = Downloader(
            1, 100000, httpx.MockTransport(lambda r: httpx.Response(200, json=subtitle()))
        )
        service = EvidenceService(settings, VideoAPI(client), downloader, store)
        try:
            await Collectors(
                settings, CollectionAPI(client), store, clock=lambda: 10000
            ).collect_at()
            event = (await store.pending_events())[0]
            candidates = await VideoAPI(client).search_candidates()
            evidence = await service.get_video(event.location.aid)
            assert candidates[0].aid == evidence.aid == event.location.aid
            assert evidence.usable and not await store.pending_actions()
            assert all(r.method == "GET" for r in requests)
        finally:
            await service.close()
            await downloader.close()


async def test_single_flight_survives_caller_cancel_and_failure_retries(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, fail = [], [False]

    async def cdn(r):
        calls.append(r)
        entered.set()
        await release.wait()
        if fail[0]:
            raise httpx.ReadTimeout("private", request=r)
        return httpx.Response(200, json=subtitle())

    async with read_client(tmp_path, VideoServer()) as (client, store, _):
        downloader = Downloader(5, 100000, httpx.MockTransport(cdn))
        service = EvidenceService(client.settings, VideoAPI(client), downloader, store)
        try:
            first = asyncio.create_task(service.get_video(1))
            await entered.wait()
            second = asyncio.create_task(service.get_video(1))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert (await second).usable and len(calls) == 1
            await store.db.execute("DELETE FROM cache")
            fail[0] = True
            with pytest.raises(NetworkFault):
                await service.get_video(1)
            fail[0] = False
            assert (await service.get_video(1)).usable and len(calls) == 3
        finally:
            await service.close()
            await downloader.close()


async def test_search_cross_page_dedup_keywords_and_markup(tmp_path):
    settings = Settings.model_validate(
        {"discovery": {"keywords": ["散步", "日常"], "pages_per_keyword": 2}}
    )

    def server(r):
        assert r.url.params["w_rid"] and r.url.params["search_type"] == "video"
        return ok(
            {
                "result": [
                    {
                        "type": "video",
                        "aid": 1,
                        "bvid": "BV1234567890",
                        "title": '<em class="keyword">散步</em> &amp; 日常',
                    }
                ]
            }
        )

    async with read_client(tmp_path, server, settings) as (client, _, _):
        candidates = await VideoAPI(client).search_candidates()
        assert len(candidates) == 1 and set(candidates[0].keywords) == {"日常", "散步"}
        assert candidates[0].title == "散步 & 日常"


@pytest.mark.parametrize("endpoint", ["details", "search", "comments", "tracks"])
async def test_malformed_video_responses_are_not_empty_success(tmp_path, endpoint):
    async with read_client(tmp_path, lambda r: ok({})) as (client, _, _):
        api = VideoAPI(client)
        methods = {
            "details": lambda: api.details(1),
            "search": lambda: api.search_page("日常", 1),
            "comments": lambda: api.comments(1),
            "tracks": lambda: api.subtitle_tracks(1, 1),
        }
        with pytest.raises(ProtocolFault):
            await methods[endpoint]()
