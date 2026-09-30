import json

import httpx
import pytest
from bili_read_fixtures import at_item, at_response, dm, read_client, session
from test_bilibili_auth import ok

from bili_comment_bot.adapters.bilibili.collection import CollectionAPI
from bili_comment_bot.adapters.bilibili.errors import NetworkFault, ProtocolFault
from bili_comment_bot.collectors import AtProgress, Collectors, DmProgress
from bili_comment_bot.config import Settings


def settings(pages=1):
    return Settings.model_validate(
        {"platform": {"max_pages": pages, "history_lookback_seconds": 1000}}
    )


async def test_at_http_locations_self_nonvideo_other_target_and_dedup(tmp_path):
    items = [
        at_item(6),
        at_item(5, root=100, parent=200),
        at_item(4, uid=42),
        at_item(3, type="dynamic", business_id=17),
        at_item(2, at_details=[{"mid": 999}]),
        at_item(1, parent=600),
    ]
    async with read_client(tmp_path, lambda r: at_response(items), settings()) as (
        client,
        store,
        requests,
    ):
        collectors = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        await collectors.collect_at()
        events = await store.pending_events()
        assert len(events) == 2
        by_parent = {e.location.parent: e for e in events}
        assert by_parent[600].location.root == 600
        assert by_parent[200].location.root == 100
        assert requests[-1].url.path == "/x/msgfeed/at"
        assert json.loads(await store.cursor("at"))["watermark"] == [9506, 6]
        audit = await (
            await store.db.execute("SELECT reason FROM audit WHERE state='ignored'")
        ).fetchone()
        assert json.loads(audit[0]) == {"self": 1, "non_video": 1, "other_target": 1}


async def test_at_budget_restart_new_insert_and_middle_failure_never_skip(tmp_path):
    stage = {"fail": True, "new": False}

    def server(r):
        if "id" not in r.url.params:
            return at_response(
                [at_item(5), at_item(4)] if stage["new"] else [at_item(4), at_item(3)], False
            )
        if r.url.params["id"] == "3":
            if stage["fail"]:
                raise httpx.ReadTimeout("private", request=r)
            return at_response([at_item(2), at_item(1)])
        return at_response([at_item(3), at_item(2)])

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        await collector.collect_at()
        incomplete = AtProgress.model_validate_json(await store.cursor("at"))
        assert incomplete.watermark == (9000, 0) and incomplete.older == (9503, 3)
        with pytest.raises(NetworkFault):
            await collector.collect_at()
        assert AtProgress.model_validate_json(await store.cursor("at")) == incomplete
        stage.update(fail=False, new=True)
        await store.close()
        await store.open()
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10100)
        await collector.collect_at()
        assert AtProgress.model_validate_json(await store.cursor("at")).watermark == (9504, 4)
        await collector.collect_at()
        assert {e.location.parent for e in await store.pending_events()} == {
            100,
            200,
            300,
            400,
            500,
        }


@pytest.mark.parametrize("damage", ["missing_root", "boolean_uid", "bad_items", "loop"])
async def test_at_bad_page_cannot_advance_watermark(tmp_path, damage):
    def server(r):
        items = [at_item(2), at_item(1)]
        if damage == "missing_root":
            del items[0]["item"]["root_id"]
        if damage == "boolean_uid":
            items[0]["user"]["mid"] = True
        if damage == "bad_items":
            return ok({"items": None, "cursor": {"is_end": True}})
        return at_response(items, damage != "loop")

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        if damage == "loop":
            await collector.collect_at()
        old = await store.cursor("at")
        with pytest.raises(ProtocolFault):
            await collector.collect_at()
        new = AtProgress.model_validate_json(await store.cursor("at"))
        assert new.watermark == (9000, 0)
        if old:
            assert await store.cursor("at") == old


async def test_page_insert_failure_rolls_back_events_and_progress(tmp_path):
    async with read_client(
        tmp_path, lambda r: at_response([at_item(2), at_item(1)]), settings()
    ) as (client, store, _):
        await store.db.execute(
            "CREATE TRIGGER fail_page BEFORE INSERT ON inbox WHEN NEW.id='comment:1:100' "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        with pytest.raises(Exception, match="injected"):
            await collector.collect_at()
        assert not await store.pending_events()
        assert AtProgress.model_validate_json(await store.cursor("at")).head is None
        await store.db.execute("DROP TRIGGER fail_page")
        await collector.collect_at()
        assert len(await store.pending_events()) == 2


async def test_discovery_jobs_transaction_failure_does_not_advance(tmp_path):
    def server(r):
        return ok({"session_list": [session(10, 3)], "has_more": 0})

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        await store.db.execute(
            "CREATE TRIGGER fail_job BEFORE INSERT ON collection_jobs "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        with pytest.raises(Exception, match="injected"):
            await collector.discover_dms()
        assert not await store.dm_jobs()
        assert json.loads(await store.cursor("dm:sessions"))["watermark_us"] == 9000_000000
        await store.db.execute("DROP TRIGGER fail_job")
        await collector.discover_dms()
        assert await store.dm_jobs() == [(10, 3)]


async def test_multi_session_paging_fair_jobs_and_restart(tmp_path):
    def server(r):
        p = r.url.params
        if r.url.path.endswith("get_sessions"):
            assert int(p["begin_ts"]) >= 8999_999999
            if "end_ts" not in p:
                return ok(
                    {
                        "session_list": [session(10, 5, 9502_000000), session(20, 6, 9501_000000)],
                        "has_more": 1,
                    }
                )
            assert int(p["end_ts"]) == 9501_000001
            return ok({"session_list": [session(30, 3, 9400_000000)], "has_more": 0})
        assert int(p["size"]) > 0 and p["w_rid"]
        uid = int(p["talker_id"])
        if uid == 10 and "end_seqno" not in p and p["begin_seqno"] == "0":
            return ok({"messages": [dm(5), dm(4, sender_uid=42, receiver_id=10)], "has_more": 1})
        if uid == 10:
            if p["begin_seqno"] == "5":
                return ok({"messages": None, "has_more": 0})
            assert p["end_seqno"] == "4"
            return ok({"messages": [dm(3, msg_type=2), dm(2), dm(1, msg_status=1)], "has_more": 0})
        return ok({"messages": [dm(6 if uid == 20 else 3, uid)], "has_more": 0})

    async with read_client(tmp_path, server, settings()) as (client, store, requests):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        await collector.collect_dms()
        assert DmProgress.model_validate_json(await store.cursor("dm:10")).watermark == 0
        assert len(await store.dm_jobs()) == 2
        await store.close()
        await store.open()
        await collector.collect_dms()  # Oldest untouched job 20 goes before the unfinished 10.
        assert DmProgress.model_validate_json(await store.cursor("dm:20")).watermark == 6
        await collector.collect_dms()
        await collector.collect_dms()
        events = await store.pending_events()
        assert {e.id for e in events} == {
            "dm:10:10005",
            "dm:10:10002",
            "dm:20:20006",
            "dm:30:30003",
        }
        assert not await store.dm_jobs()
        assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize("damage", ["bad_content", "wrong_receiver", "empty_more", "same_seq"])
async def test_dm_malformed_or_nonprogressing_page_keeps_job(tmp_path, damage):
    def server(r):
        if r.url.path.endswith("get_sessions"):
            return ok({"session_list": [session(10, 3)], "has_more": 0})
        raw = dm(3)
        if damage == "bad_content":
            raw["content"] = "broken"
        if damage == "wrong_receiver":
            raw["receiver_id"] = 999
        return ok({"messages": [] if damage == "empty_more" else [raw], "has_more": 1})

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        if damage == "same_seq":
            await collector.collect_dms()
        with pytest.raises(ProtocolFault):
            await collector.collect_dms()
        assert await store.dm_jobs() == [(10, 3)]
        assert DmProgress.model_validate_json(await store.cursor("dm:10")).watermark == 0


async def test_initial_cutoff_persists_across_failure_and_ignores_old_history(tmp_path):
    fail = [True]

    def server(r):
        if fail[0]:
            raise httpx.ReadTimeout("private", request=r)
        if r.url.path.endswith("get_sessions"):
            return ok({"session_list": [session(10, 2)], "has_more": 0})
        return ok({"messages": [dm(2, timestamp=9100), dm(1, timestamp=8900)], "has_more": 0})

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        with pytest.raises(NetworkFault):
            await collector.collect_dms()
        fail[0] = False
        collector.clock = lambda: 12000
        await collector.collect_dms()
        assert [e.id for e in await store.pending_events()] == ["dm:10:10002"]
        assert DmProgress.model_validate_json(await store.cursor("dm:10")).watermark == 2


async def test_discovery_failure_and_poison_job_do_not_starve_other_jobs(tmp_path):
    failing_discovery = [False]

    def server(r):
        if r.url.path.endswith("get_sessions"):
            if failing_discovery[0]:
                raise httpx.ReadTimeout("private", request=r)
            return ok(
                {"session_list": [session(10, 2), session(20, 2), session(30, 2)], "has_more": 0}
            )
        uid = int(r.url.params["talker_id"])
        return ok(
            {
                "messages": [dm(2, uid, content="broken" if uid == 10 else '{"content":"聊聊"}')],
                "has_more": 0,
            }
        )

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        with pytest.raises(ProtocolFault):
            await collector.collect_dms()
        failing_discovery[0] = True
        with pytest.raises(NetworkFault):
            await collector.collect_dms()
        with pytest.raises(NetworkFault):
            await collector.collect_dms()
        assert {event.uid for event in await store.pending_events()} == {20, 30}
        assert await store.dm_jobs() == [(10, 2)]
        assert DmProgress.model_validate_json(await store.cursor("dm:10")).watermark == 0


async def test_new_messages_while_old_dm_scan_incomplete_are_collected_next(tmp_path):
    target = [3]

    def server(r):
        if r.url.path.endswith("get_sessions"):
            return ok(
                {"session_list": [session(10, target[0], 9500_000000 + target[0])], "has_more": 0}
            )
        p = r.url.params
        if "end_seqno" in p:
            return ok({"messages": [dm(1)], "has_more": 0})
        if p["begin_seqno"] == "0":
            return ok({"messages": [dm(3), dm(2)], "has_more": 1})
        return ok({"messages": [dm(4)], "has_more": 0})

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        await collector.collect_dms()
        target[0] = 4
        await collector.collect_dms()
        assert await store.dm_jobs() == [(10, 4)]
        assert DmProgress.model_validate_json(await store.cursor("dm:10")).watermark == 3
        await collector.collect_dms()
        assert not await store.dm_jobs()
        assert len(await store.pending_events()) == 4


async def test_equal_session_timestamp_boundary_stops_without_dropping_jobs(tmp_path):
    def server(r):
        return ok({"session_list": [session(10, 2), session(20, 2)], "has_more": 1})

    async with read_client(tmp_path, server, settings()) as (client, store, _):
        collector = Collectors(client.settings, CollectionAPI(client), store, clock=lambda: 10000)
        await collector.discover_dms()
        saved = await store.cursor("dm:sessions")
        with pytest.raises(ProtocolFault):
            await collector.discover_dms()
        assert await store.cursor("dm:sessions") == saved
        assert await store.dm_jobs() == [(10, 2), (20, 2)]
