"""Production runtime wiring with synthetic HTTP; no real account/media/model claims."""

import asyncio
import json
import logging
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from ai_fixtures import FixtureAI, ai_settings, video_evidence
from bili_read_fixtures import at_item, at_response, dm, session, video_details
from pydantic import SecretStr
from test_bilibili_auth import cookie_headers, credentials, nav, ok
from test_bilibili_transport import no_wait
from test_business_flows import business, enqueue
from transcription_fixtures import audio_bytes, audio_response, transcript_response

from bili_comment_bot.adapters.bilibili.auth import AuthManager
from bili_comment_bot.adapters.bilibili.auth_state import CredentialFile, RefreshPhase
from bili_comment_bot.adapters.bilibili.errors import (
    AuthFault,
    CaptchaRequired,
    IdentityMismatch,
    LoginExpired,
    NetworkFault,
    ReauthenticationRequired,
)
from bili_comment_bot.adapters.bilibili.transport import BiliTransport
from bili_comment_bot.ai.client import AIClient, AIError
from bili_comment_bot.ai.prompts import POLICY_VERSION, PROMPT_VERSION
from bili_comment_bot.domain import (
    ActionKind,
    ActionStatus,
    Channel,
    Decision,
    LikeStateEvidence,
    PublishAction,
    VideoScore,
)
from bili_comment_bot.instance_lock import InstanceInUse, InstanceLock
from bili_comment_bot.observability import log_result, read_status, write_private
from bili_comment_bot.operations import operate
from bili_comment_bot.runtime import RuntimeIO, Scheduler, run_bot
from bili_comment_bot.storage import Store
from bili_comment_bot.work import DiscoveryWorkflow, WorkBatch, WorkResult, WorkState


class RuntimeServer:
    def __init__(self, *, renew=False):
        self.requests, self.renew = [], renew
        self.at_fault = None
        self.with_candidates = True

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/nav"):
            return ok(nav())
        if path.endswith("/cookie/info"):
            return ok({"refresh": self.renew, "timestamp": 10000000})
        if "/correspond/" in path:
            return httpx.Response(200, text='<div id="1-name">refresh-csrf</div>')
        if path.endswith("/cookie/refresh"):
            self.renew = False
            return ok({"refresh_token": "new-token"}, cookie_headers())
        if path.endswith("/confirm/refresh"):
            return ok(None)
        if path.endswith("/at"):
            if self.at_fault:
                return httpx.Response(
                    200, json={"code": self.at_fault, "message": "private-session"}
                )
            return at_response([at_item(2)])
        if path.endswith("/get_sessions") or path.endswith("/new_sessions"):
            return ok({"session_list": [session(10, 1)], "has_more": 0})
        if path.endswith("/fetch_session_msgs"):
            return ok({"messages": [dm(1)], "has_more": 0})
        if path.endswith("/acc/relation"):
            return ok(
                {
                    "relation": {"mid": 10, "attribute": 0},
                    "be_relation": {"mid": 42, "attribute": 2},
                }
            )
        if path.endswith("/acc/info"):
            return ok({"mid": 123, "name": "朋友"})
        if path.endswith("/view"):
            return ok(video_details())
        if path.endswith("/search/type"):
            return ok(
                {
                    "result": [{"aid": 1, "type": "video", "bvid": "BV1234567890", "title": "日常"}]
                    if self.with_candidates
                    else []
                }
            )
        if path.endswith("/reply"):
            return ok(
                {
                    "page": {"count": 3},
                    "replies": [
                        {"content": {"message": value}} for value in ["有趣", "温暖", "很抽象"]
                    ],
                }
            )
        if path.endswith("/wbi/v2"):
            return ok({"aid": 1, "cid": 1, "subtitle": {"subtitles": []}})
        if path.endswith("/playurl"):
            return ok(audio_response())
        pytest.fail("unexpected HTTP path: " + path)


def runtime_settings(tmp_path, **overrides):
    settings = ai_settings(
        evidence={"transcription_enabled": True},
        transcription={"api_key": "speech-fixture-key", "base_url": "https://speech.example/v1"},
        discovery={"keywords": ["日常"]},
        **overrides,
    )
    settings.data_dir = tmp_path
    return settings


def io_for(server, fixture, *, speech_calls=None, stop=None):
    def speech(request):
        if speech_calls is not None:
            speech_calls.append(request)
        return httpx.Response(200, json=transcript_response())

    return RuntimeIO(
        platform=httpx.MockTransport(server),
        model=httpx.MockTransport(fixture),
        download=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes())),
        transcription=httpx.MockTransport(speech),
        read_wait=no_wait,
        write_wait=no_wait,
        wall_clock=lambda: 10000,
    )


async def test_run_once_full_real_adapters_refresh_collect_dm_summon_audio_ai_discovery_restart(
    tmp_path,
):
    settings = runtime_settings(tmp_path)
    CredentialFile(tmp_path / "auth.json").save(credentials())
    server, fixture, speech = RuntimeServer(renew=True), FixtureAI(), []
    await run_bot(
        settings, once=True, io=io_for(server, fixture, speech_calls=speech), install_signals=False
    )
    assert {"companion", "summary", "rating", "invite"} <= set(fixture.purposes)
    assert len(speech) == 1  # Event and discovery share validated content cache.
    content_posts = [
        r for r in server.requests if r.method == "POST" and "passport" not in r.url.host
    ]
    assert content_posts == []
    auth_posts = [r.url.path for r in server.requests if r.method == "POST"]
    assert auth_posts == [
        "/x/passport-login/web/cookie/refresh",
        "/x/passport-login/web/confirm/refresh",
    ]
    state = read_status(tmp_path, "sim", now=lambda: 10000)
    assert not state["alive"] and not state["healthy"] and not state["needs_login"]
    assert state["counts"]["actions"] == {"simulated": 3}
    assert state["counts"]["workflow_states"] == {"done": 1}
    assert state["evidence"]["cache_hits"] >= 1
    assert state["ai"]["requests"] > 0 and state["transcription"]["requests"] == 1
    for path in (tmp_path / "state.db", tmp_path / "status-sim.json", tmp_path / "auth.json"):
        assert os.stat(path).st_mode & 0o777 == 0o600
    count = len(fixture.calls)
    server.with_candidates = False
    await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    assert len(fixture.calls) == count  # No successful reply/action regenerated.
    with InstanceLock(tmp_path):
        pass


@pytest.mark.parametrize(
    "phase",
    [RefreshPhase.CONFIRM_PENDING, RefreshPhase.REFRESH_STARTED, RefreshPhase.CONFIRM_STARTED],
)
async def test_runtime_startup_refresh_phase(tmp_path, phase):
    settings, server, fixture = runtime_settings(tmp_path), RuntimeServer(), FixtureAI()
    CredentialFile(tmp_path / "auth.json").save(credentials(phase=phase))
    if phase == RefreshPhase.CONFIRM_PENDING:
        await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
        assert CredentialFile(tmp_path / "auth.json").load().phase == RefreshPhase.STABLE
        assert any(r.url.path.endswith("/confirm/refresh") for r in server.requests)
    else:
        with pytest.raises(ReauthenticationRequired):
            await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
        assert not server.requests and not fixture.calls
        assert read_status(tmp_path, "sim", now=lambda: 10000)["needs_login"]
    with InstanceLock(tmp_path):
        pass


@pytest.mark.parametrize("code,kind", [(-101, LoginExpired), (-105, CaptchaRequired)])
async def test_runtime_auth_notification_stops_new_requests_even_if_nested_business_swallows(
    tmp_path, code, kind
):
    settings, server, fixture = runtime_settings(tmp_path), RuntimeServer(), FixtureAI()
    CredentialFile(tmp_path / "auth.json").save(credentials())
    server.at_fault = code
    with pytest.raises(kind):
        await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    assert server.requests[-1].url.path.endswith("/at") and not fixture.calls
    status = read_status(tmp_path, "sim", now=lambda: 10000)
    assert status["needs_login"] and not status["ready"] and status["auth_error"] == kind.__name__
    with InstanceLock(tmp_path):
        pass


async def test_runtime_missing_key_init_closes_earlier_client_and_releases_lock(
    tmp_path, monkeypatch
):
    settings, fixture, closed = runtime_settings(tmp_path), FixtureAI(), []
    settings.transcription.api_key = SecretStr("")
    original = AIClient.close

    async def close(self):
        closed.append(True)
        await original(self)

    monkeypatch.setattr(AIClient, "close", close)
    with pytest.raises(AIError):
        await run_bot(
            settings, once=True, io=io_for(RuntimeServer(), fixture), install_signals=False
        )
    assert closed == [True] and not (tmp_path / "state.db").exists()
    with InstanceLock(tmp_path):
        pass


async def test_runtime_lock_conflict_before_any_client_or_database(tmp_path):
    settings = runtime_settings(tmp_path)
    with InstanceLock(tmp_path), pytest.raises(InstanceInUse):
        await run_bot(
            settings, once=True, io=io_for(RuntimeServer(), FixtureAI()), install_signals=False
        )
    assert not (tmp_path / "state.db").exists()


async def test_runtime_identity_mismatch_zero_business(tmp_path):
    settings, fixture = runtime_settings(tmp_path), FixtureAI()
    settings.platform.bot_uid = 43
    CredentialFile(tmp_path / "auth.json").save(credentials())
    server = RuntimeServer()
    with pytest.raises(IdentityMismatch):
        await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    assert len(server.requests) == 1 and not fixture.calls


def approved(key, *, dependency=None, kind=ActionKind.ENCOURAGE):
    return PublishAction(
        id=key,
        kind=kind,
        aid=1,
        dependency=dependency,
        text="" if kind == ActionKind.LIKE else "今天也很温暖",
        input_decision=Decision.ALLOW,
        output_safe=True,
        evidence_usable=True,
    )


async def test_schema4_migration_durable_due_candidates_isolation_and_dependency_fairness(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE workflows(ns TEXT,id TEXT,payload TEXT,PRIMARY KEY(ns,id));")
    db.execute("INSERT INTO workflows VALUES(?,?,?)", ("live", "discovery:9", flow_payload(9)))
    db.commit()
    db.close()
    now = [10000]
    store = await Store(path, "sim", clock=lambda: now[0]).open()
    try:
        assert await store.due_workflows(2) == []
        await store.enqueue_candidates([1, 2, 1])
        assert await store.due_candidates(1) == [1]
        await store.reevaluate_candidate(1, 60)
        await store.enqueue_candidates([1])  # Discovery cannot reset cooldown.
        assert await store.due_candidates(5) == [2]
        now[0] += 60
        assert await store.due_candidates(5) == [2, 1]
        await store.put_actions(
            [
                approved("blocked-dependency"),
                approved("a", dependency="blocked-dependency"),
                approved("z"),
            ]
        )
        await store.claim_action("blocked-dependency")
        await store.finish_action("blocked-dependency", ActionStatus.UNCERTAIN)
        assert [a.id for a in await store.pending_actions(1)] == ["z"]
    finally:
        await store.close()
    live = await Store(path, "live").open()
    try:
        assert await live.due_workflows(5) == [9]
        assert await live.due_candidates(5) == []
    finally:
        await live.close()


async def test_resume_flow_not_in_search_after_uncertain_verification_without_regeneration(
    tmp_path,
):
    async with business(tmp_path) as (service, store, fixture, server, _, _):
        server.uncertain_like = True
        assert await service.discover_video(1) == [ActionStatus.UNCERTAIN]
        assert await store.due_workflows(5) == []
        assert (await store.counts())["workflow_states"] == {"paused": 1}
        await store.resolve_uncertain(
            "discovery:1:like",
            None,
            "已在对应账号核实点赞",
            like_state=LikeStateEvidence(account_uid=42, aid=1, liked=True),
        )
        assert await store.due_workflows(5) == [1]
        count = fixture.purposes.count("rating")
        server.uncertain_like = False
        assert await service.discover_video(1) == [ActionStatus.SUCCEEDED] * 3
        assert fixture.purposes.count("rating") == count
        assert await store.due_workflows(5) == []
        assert (await store.counts())["work_retries"] == 0


async def test_low_rating_and_missing_evidence_have_explicit_reevaluation_cooldown(tmp_path):
    async with business(tmp_path) as (service, store, fixture, _, evidence, now):
        await store.enqueue_candidates([1])
        fixture.recommendation = fixture.absurdity = 0.0
        assert await service.discover_video(1) == []
        assert await store.due_candidates(5) == []
        now[0] += service.settings.discovery.interval
        assert await store.due_candidates(5) == [1]
        evidence.value = evidence.value.model_copy(update={"complete": False})
        assert await service.discover_video(1) == []
        assert await store.due_candidates(5) == []


async def test_business_follow_error_latched_without_blocking_pending_payload(tmp_path):
    async with business(tmp_path) as (service, store, fixture, server, _, _):
        fault = AuthFault()
        service.platform.transport.auth_fault = fault

        def bad(request):
            return httpx.Response(200, json={"code": -101})

        await service.platform.transport.client.aclose()
        service.platform.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(bad))
        event = await enqueue(store)
        assert await service.process_event(event.id) is None
        assert fault.kind is LoginExpired and not fixture.calls
        await store.put_actions(
            [
                PublishAction(
                    id="reply2",
                    kind=ActionKind.REPLY,
                    uid=10,
                    channel=Channel.DM,
                    text="温柔回复",
                    input_decision=Decision.ALLOW,
                    output_safe=True,
                )
            ]
        )
        assert (
            await service.dispatcher.execute(
                PublishAction.model_validate_json((await store.action("reply2"))["payload"])
            )
            == ActionStatus.PENDING
        )


async def test_ambiguous_write_not_released_when_captcha_notification_arrives(tmp_path):
    async with business(tmp_path) as (service, store, _, server, _, _):
        fault = AuthFault()
        service.platform.transport.auth_fault = fault
        original = server.__call__

        def handler(request):
            if request.method == "POST":
                return ok({"v_voucher": "private-voucher"})
            return original(request)

        await service.platform.transport.client.aclose()
        service.platform.transport.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        event = await enqueue(store)
        assert await service.process_event(event.id) == ActionStatus.UNCERTAIN
        assert fault.kind is CaptchaRequired
        assert not await store.quota_available(10, Channel.DM, 1, [])
        await store.recover()
        assert (await store.action("reply:" + event.id))["status"] == ActionStatus.UNCERTAIN


async def test_unknown_refresh_fault_notifies_reauthentication_and_no_retry(tmp_path):
    settings, fault, calls = ai_settings(), AuthFault(), []
    file = CredentialFile(tmp_path / "auth.json")
    file.save(credentials())
    server = RuntimeServer(renew=True)

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/cookie/refresh"):
            raise httpx.ReadTimeout("private-session", request=request)
        return server(request)

    transport = BiliTransport(
        settings,
        httpx.MockTransport(handler),
        read_wait=no_wait,
        write_wait=no_wait,
        auth_fault=fault,
    )
    try:
        auth = AuthManager(settings, transport, file)
        with pytest.raises(NetworkFault):
            await auth.refresh()
        assert fault.kind is ReauthenticationRequired
        with pytest.raises(ReauthenticationRequired):
            await auth.refresh()
        assert sum(r.method == "POST" for r in calls) == 1
    finally:
        await transport.close()


async def test_operations_namespace_lock_listing_privacy_and_verified_receipt(tmp_path):
    settings = ai_settings()
    settings.data_dir = tmp_path
    store = await Store(tmp_path / "state.db", "live").open()
    try:
        await store.bind_account(42)
        action = PublishAction(
            id="private-action",
            kind=ActionKind.REPLY,
            uid=10,
            channel=Channel.DM,
            text="private-message",
            input_decision=Decision.ALLOW,
            output_safe=True,
        )
        await store.put_actions(
            [action, approved("like", kind=ActionKind.LIKE), approved("cancel")]
        )
        await store.claim_action(action.id)
        await store.finish_action(action.id, ActionStatus.UNCERTAIN)
        await store.claim_action("like")
        await store.finish_action("like", ActionStatus.UNCERTAIN)
    finally:
        await store.close()
    listing = await operate(settings, "live", "actions", uncertain=True)
    assert len(listing["actions"]) == 2 and "private-message" not in json.dumps(listing)
    assert (await operate(settings, "sim", "actions"))["actions"] == []
    with InstanceLock(tmp_path), pytest.raises(InstanceInUse):
        await operate(settings, "live", "cancel-action", action_id="cancel")
    with pytest.raises(ValueError):
        await operate(settings, None, "actions")
    with pytest.raises(ValueError):
        await operate(
            settings, "live", "verify-action", action_id=action.id, remote_id="fake", note="核实"
        )
    with pytest.raises(ValueError):
        await operate(
            settings,
            "live",
            "verify-action",
            action_id="like",
            account_uid=43,
            aid=1,
            liked=True,
            note="核实",
        )
    await operate(
        settings,
        "live",
        "verify-action",
        action_id=action.id,
        remote_id="1234",
        note="已核实真实私信回执",
    )
    await operate(
        settings,
        "live",
        "verify-action",
        action_id="like",
        account_uid=42,
        aid=1,
        liked=True,
        note="已核实点赞",
    )
    assert (await operate(settings, "live", "cancel-action", action_id="cancel"))["cancelled"]
    assert not (await operate(settings, "live", "cancel-action", action_id=action.id))["cancelled"]
    db = sqlite3.connect(tmp_path / "state.db")
    assert db.execute("SELECT COUNT(*) FROM audit WHERE state='succeeded'").fetchone()[0] == 2
    db.close()


def test_allowlist_logs_and_private_stale_status_do_not_contain_injected_data(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="bili_comment_bot.runtime"):
        log_result(
            "dm",
            "failed",
            "live",
            error=AIError("private-message cookie secret URL?signature=secret"),
        )
    assert "AIError" in caplog.text
    assert not any(
        word in caplog.text for word in ["private-message", "cookie", "signature", "secret"]
    )
    with pytest.raises(ValueError):
        log_result("private-message", "failed", "live")
    write_private(
        tmp_path / "status-live.json",
        {
            "mode": "live",
            "updated_at": 1,
            "stale_after": 10,
            "alive": True,
            "ready": True,
            "business_health": "normal",
        },
    )
    assert read_status(tmp_path, "live", now=lambda: 2)["healthy"]
    assert not read_status(tmp_path, "live", now=lambda: 12)["healthy"]
    assert os.stat(tmp_path / "status-live.json").st_mode & 0o777 == 0o600


def scheduler_for(settings, store, *, stop=None, io=None, fault=None, business_service=None):
    async def empty(*args):
        pass

    collectors = SimpleNamespace(collect_at=empty, collect_dms=empty)
    service = business_service or SimpleNamespace(
        process_event=empty, discover_video=empty, dispatcher=SimpleNamespace(execute=empty)
    )
    return Scheduler(
        settings,
        store,
        collectors,
        SimpleNamespace(search_candidates=empty),
        service,
        SimpleNamespace(refresh=empty),
        fault or AuthFault(),
        stop=stop,
        io=io,
    )


async def test_bounded_pool_no_overlap_and_independent_jobs_with_slow_collector(tmp_path):
    settings = ai_settings(limits={"concurrency": 2})
    settings.runtime.shutdown_timeout = 0.1
    store = await Store(tmp_path / "state.db", "sim").open()
    stop, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scheduler = scheduler_for(settings, store, stop=stop)
    active, maximum, started = 0, 0, []

    async def blocked(value):
        nonlocal active, maximum
        started.append(value)
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return WorkResult(WorkState.COMPLETED)

    try:
        batch = asyncio.create_task(scheduler._map(range(6), blocked))
        await entered.wait()
        assert len(started) == 2
        release.set()
        await batch
        assert maximum == 2 and len(started) == 6
        slow_entered, finish = asyncio.Event(), asyncio.Event()

        async def slow():
            slow_entered.set()
            await finish.wait()

        async def independent():
            await slow_entered.wait()
            stop.set()
            finish.set()

        scheduler.jobs = lambda: {"at": (slow, 30), "dm": (independent, 30)}
        await scheduler.run()
        assert set(scheduler.last_success) == {"at", "dm"}
    finally:
        await store.close()


async def test_scheduler_finite_backoff_injected_clock_and_other_channel_success(tmp_path):
    settings = ai_settings()
    store = await Store(tmp_path / "state.db", "sim").open()
    now = [100]
    scheduler = scheduler_for(
        settings, store, io=RuntimeIO(monotonic=lambda: now[0], wall_clock=lambda: now[0])
    )

    async def bad():
        now[0] += 1
        raise NetworkFault()

    async def good():
        now[0] += 1

    try:
        assert [await scheduler.step("at", bad) for _ in range(8)] == [
            5,
            10,
            20,
            40,
            80,
            160,
            160,
            160,
        ]
        assert await scheduler.step("dm", good) == 0
        assert scheduler.last_success["dm"] == now[0]
        assert scheduler.failures["at"] == 6
        assert await scheduler.step("at", good) == 0 and scheduler.failures["at"] == 0
    finally:
        await store.close()


async def test_scheduler_uncertain_post_shutdown_persists_quota_and_restart_never_resends(tmp_path):
    async with business(tmp_path) as (service, store, _, server, _, _):
        service.settings.runtime.shutdown_timeout = 0.01
        entered, stop = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.method == "POST":
                entered.set()
                await asyncio.Event().wait()
            return server(request)

        await service.platform.transport.client.aclose()
        service.platform.transport.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        event = await enqueue(store)
        # Persist a validated reply first so only the writer runs in this test.
        await store.put_actions(
            [
                PublishAction(
                    id="reply:" + event.id,
                    kind=ActionKind.REPLY,
                    uid=10,
                    channel=Channel.DM,
                    text="陪着你",
                    input_decision=Decision.ALLOW,
                    output_safe=True,
                )
            ]
        )
        scheduler = scheduler_for(service.settings, store, stop=stop, business_service=service)
        scheduler.jobs = lambda: {"actions": (scheduler.actions, 2)}
        running = asyncio.create_task(scheduler.run())
        await entered.wait()
        stop.set()
        await running
        assert (await store.action("reply:" + event.id))["status"] == ActionStatus.UNCERTAIN
        assert not await store.quota_available(10, Channel.DM, 1, [])
        await store.recover()
        assert await service.resume_actions() == []


async def test_continuous_runtime_cancel_generation_releases_resources_and_recovers_event(tmp_path):
    settings, server, fixture = runtime_settings(tmp_path), RuntimeServer(), FixtureAI()
    settings.runtime.shutdown_timeout = 0.01
    CredentialFile(tmp_path / "auth.json").save(credentials())
    entered, stop = asyncio.Event(), asyncio.Event()
    original = fixture.__call__

    async def delayed(request):
        data = json.loads(request.content)
        if "purpose=companion" in data["messages"][0]["content"]:
            entered.set()
            await asyncio.Event().wait()
        return original(request)

    io = io_for(server, fixture)
    io.model = httpx.MockTransport(delayed)
    running = asyncio.create_task(run_bot(settings, io=io, stop=stop, install_signals=False))
    await asyncio.wait_for(entered.wait(), 10)
    stop.set()
    await running
    with InstanceLock(tmp_path):
        pass
    store = await Store(tmp_path / "state.db", "sim", clock=lambda: 10000).open()
    try:
        assert await store.event("dm:10:10001") is not None
        await store.recover()
        assert len(await store.pending_events(20)) >= 1
    finally:
        await store.close()
    await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    status = read_status(tmp_path, "sim", now=lambda: 10000)
    assert status["counts"]["inbox"] == {"done": 2}


def test_operations_cli_requires_namespace_and_prints_safe_configuration_error(
    tmp_path, monkeypatch, capsys
):
    from bili_comment_bot.__main__ import main

    config = tmp_path / "config.toml"
    config.write_text("data_dir = " + json.dumps(str(tmp_path)) + "\n")
    monkeypatch.setattr("sys.argv", ["bili-comment-bot", "--config", str(config), "actions"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and "--namespace" in capsys.readouterr().err


async def test_failed_store_initialization_closes_connection_without_leaking(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE inbox(ns TEXT, id TEXT);")
    db.close()
    store = Store(path, "sim")
    with pytest.raises(sqlite3.OperationalError):
        await store.open()
    assert store.db is None


def flow_payload(aid):
    return DiscoveryWorkflow(
        aid=aid,
        evidence=video_evidence(aid=aid),
        score=VideoScore(
            heat=0.0,
            recommendation=99.0,
            absurdity=99.0,
            reasons=["热度依据", "推荐依据", "抽象依据"],
        ),
        heat={"score": 0.0},
        invite_uids=[123],
        policy_version=POLICY_VERSION,
        prompt_version=PROMPT_VERSION,
        model="fixture-model",
    ).model_dump_json()


async def test_discovery_unexpected_failures_are_typed_backoff_persistent_and_fair(tmp_path):
    settings = ai_settings(runtime={"batch_size": 2})
    now = [10000]
    path = tmp_path / "state.db"
    store = await Store(path, "sim", clock=lambda: now[0]).open()
    calls = []

    async def handle(aid):
        calls.append(aid)
        if aid in {1, 2}:
            raise RuntimeError("private failed workflow text")
        await store.discovery_state(f"discovery:{aid}", "done")
        return WorkResult(WorkState.COMPLETED)

    service = SimpleNamespace(discover_video_result=handle)
    scheduler = scheduler_for(settings, store, business_service=service)
    try:
        for aid in (1, 2, 3):
            await store.put_workflow(f"discovery:{aid}", flow_payload(aid))
        assert await scheduler.step("discovery", scheduler.discovery) == 5
        assert scheduler.failures["discovery"] == 1
        assert scheduler.batches["discovery"]["failed"] == 2
        assert await store.due_workflows(2) == [3]
        await scheduler.step("discovery", scheduler.discovery)
        assert calls == [1, 2, 3]
        assert await store.due_workflows(2) == []
    finally:
        await store.close()
    reopened = await Store(path, "sim", clock=lambda: now[0]).open()
    try:
        assert await reopened.due_workflows(2) == []
        now[0] += 30
        assert await reopened.due_workflows(2) == [1, 2]

        async def good(aid):
            await reopened.discovery_state(f"discovery:{aid}", "done")
            return WorkResult(WorkState.COMPLETED)

        service.discover_video_result = good
        scheduler = scheduler_for(settings, reopened, business_service=service)
        assert await scheduler.step("discovery", scheduler.discovery) == 0
        assert (await reopened.counts())["work_retries"] == 0
    finally:
        await reopened.close()


async def test_action_exception_backoff_does_not_reset_unknown_or_retry_before_due(tmp_path):
    settings, now = ai_settings(), [10000]
    store = await Store(tmp_path / "state.db", "sim", clock=lambda: now[0]).open()
    calls = []

    async def fail(action):
        calls.append(action.id)
        if action.id == "a":
            raise RuntimeError("private")
        await store.claim_action(action.id)
        await store.finish_action(action.id, ActionStatus.SIMULATED)
        return ActionStatus.SIMULATED

    service = SimpleNamespace(dispatcher=SimpleNamespace(execute=fail))
    scheduler = scheduler_for(settings, store, business_service=service)
    try:
        await store.put_actions([approved("a"), approved("b")])
        assert await scheduler.step("actions", scheduler.actions) == 5
        assert calls == ["a", "b"]
        assert scheduler.batches["actions"] == dict(
            completed=1, skipped=0, deferred=0, failed=1, attention=0
        )
        assert await store.pending_actions() == []
        await scheduler.step("actions", scheduler.actions)
        assert calls == ["a", "b"] and scheduler.failures["actions"] == 1
        now[0] += 30
        assert [a.id for a in await store.pending_actions()] == ["a"]
        await store.claim_action("a")
        await store.finish_action("a", ActionStatus.UNCERTAIN)
        assert not await store.pending_actions()
        assert (await store.counts())["work_retries"] == 0
        await store.recover()
        assert (await store.action("a"))["status"] == ActionStatus.UNCERTAIN
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["events", "actions", "discovery"])
async def test_invalid_payload_quarantines_one_record_preserves_raw_and_processes_others(
    tmp_path, kind
):
    async with business(tmp_path, live=False) as (service, store, fixture, _, _, _):
        if kind == "events":
            await enqueue(store, key="a-bad")
            await enqueue(store, key="z-good")
            table, key = "inbox", "a-bad"
        elif kind == "actions":
            await store.put_actions([approved("a-bad"), approved("z-good")])
            table, key = "actions", "a-bad"
        else:
            await store.put_workflow("discovery:1", flow_payload(1))
            await store.put_workflow("discovery:2", flow_payload(2))
            table, key = "workflows", "discovery:1"

            async def complete(aid):
                assert aid == 2
                await store.discovery_state("discovery:2", "done")
                return WorkResult(WorkState.COMPLETED)

            service.discover_video_result = complete
        async with store.transaction() as db:
            await db.execute(
                f"UPDATE {table} SET payload=? WHERE ns=? AND id=?",
                ("private-broken", store.ns, key),
            )
        scheduler = scheduler_for(service.settings, store, business_service=service)
        callback = getattr(scheduler, kind)
        assert await scheduler.step(kind, callback) == 5
        assert scheduler.batches[kind]["attention"] == 1
        assert scheduler.batches[kind]["completed"] == 1
        async with store.transaction() as db:
            raw = await (
                await db.execute(
                    f"SELECT payload FROM {table} WHERE ns=? AND id=?", (store.ns, key)
                )
            ).fetchone()
            assert raw[0] == "private-broken"
            audit = await (
                await db.execute("SELECT reason FROM audit WHERE state='quarantined'")
            ).fetchone()
            assert audit[0] == "invalid_payload"
        assert (await store.counts())["quarantined"] == 1
        assert await scheduler.step(kind, callback) == 0
        assert scheduler.batches[kind]["attention"] == 0
        assert scheduler.health(await store.counts(), alive=True, ready=True)[0] == "attention"


async def test_once_reports_real_ai_failure_but_normal_rejection_low_score_and_no_work_pass(
    tmp_path,
):
    settings, server, fixture = runtime_settings(tmp_path), RuntimeServer(), FixtureAI()
    CredentialFile(tmp_path / "auth.json").save(credentials())
    fixture.fail_purposes = {"companion"}
    with pytest.raises(RuntimeError):
        await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    state = read_status(tmp_path, "sim", now=lambda: 10000)
    assert state["job_failures"]["events"] == 1
    assert state["batches"]["events"]["deferred"] == 1
    fixture.fail_purposes = set()
    fixture.recommendation = fixture.absurdity = 0.0
    # Not-due deferred work is a normal no-op, not another failure of this --once pass.
    await run_bot(settings, once=True, io=io_for(server, fixture), install_signals=False)
    assert read_status(tmp_path, "sim", now=lambda: 10000)["counts"]["work_retries"] == 1


async def test_explicit_business_outcomes_distinguish_skip_rejection_and_defer(tmp_path):
    async with business(tmp_path, live=False) as (service, store, fixture, server, _, _):
        event = await enqueue(store, key="refusal", text="请帮我写代码")
        assert (await service.process_event_result(event.id)).state == WorkState.COMPLETED
        assert (await service.process_event_result(event.id)).state == WorkState.SKIPPED
        server.follows = False
        event = await enqueue(store, key="unfollowed")
        assert (await service.process_event_result(event.id)).state == WorkState.SKIPPED
        server.follows = True
        fixture.fail_purposes.add("companion")
        event = await enqueue(store, key="deferred")
        assert (await service.process_event_result(event.id)).state == WorkState.DEFERRED
        assert (await service.process_event_result(event.id)).state == WorkState.SKIPPED


@pytest.mark.parametrize("reason", ["unfollowed", "quota"])
async def test_deferred_event_ignored_on_retry_clears_retry_across_reopen(tmp_path, reason):
    async with business(tmp_path, live=False, overrides={"limits": {"dm_per_hour": 1}}) as (
        service,
        store,
        fixture,
        server,
        _,
        now,
    ):
        fixture.fail_purposes.add("companion")
        event = await enqueue(store, key="retry-terminal")
        assert (await service.process_event_result(event.id)).state == WorkState.DEFERRED
        assert (await store.counts())["work_retries"] == 1
        # Pending is not terminal and must retain the durable cooldown.
        await store.finish_event(event.id, "pending")
        assert not await store.ready_work(event.id)
        fixture.fail_purposes.clear()
        if reason == "unfollowed":
            server.follows = False
        else:
            other = await enqueue(store, key="consume-quota")
            assert (await service.process_event_result(other.id)).state == WorkState.COMPLETED
        now[0] += 31
        assert (await service.process_event_result(event.id)).state == WorkState.SKIPPED
        assert (await store.counts())["work_retries"] == 0
    reopened = await Store(tmp_path / "state.db", "sim").open()
    try:
        counts = await reopened.counts()
        assert counts["work_retries"] == 0
        scheduler = scheduler_for(service.settings, reopened)
        scheduler.last_success = dict.fromkeys(scheduler.jobs(), 10000)
        assert scheduler.health(counts, alive=True, ready=True)[0] == "normal"
    finally:
        await reopened.close()


async def test_business_health_startup_disabled_jobs_failure_empty_poll_and_recovery(tmp_path):
    settings = ai_settings(discovery={"invite_uids": [], "keywords": []})
    store = await Store(tmp_path / "state.db", "sim").open()
    scheduler = scheduler_for(settings, store)
    try:
        counts = await store.counts()
        assert "search" not in scheduler.jobs() and "discovery" not in scheduler.jobs()
        assert scheduler.health(counts, alive=True, ready=True)[0] == "starting"
        for name in scheduler.jobs():
            scheduler.last_success[name] = 10000
        assert scheduler.health(counts, alive=True, ready=True) == ("normal", [])

        async def bad():
            raise NetworkFault()

        assert await scheduler.step("at", bad) == 5
        assert scheduler.health(counts, alive=True, ready=True) == ("degraded", ["at_failed"])

        async def good():
            return 0

        assert await scheduler.step("at", good) == 0
        assert scheduler.health(counts, alive=True, ready=True)[0] == "normal"

        async def failed_batch():
            batch = WorkBatch()
            batch.add(WorkResult(WorkState.DEFERRED))
            return batch

        await scheduler.step("events", failed_batch)

        async def empty():
            return WorkBatch()

        await scheduler.step("events", empty)
        assert scheduler.failures["events"] == 1  # No empty-tick fake recovery.

        async def recovered():
            batch = WorkBatch()
            batch.add(WorkResult(WorkState.COMPLETED))
            return batch

        await scheduler.step("events", recovered)
        assert scheduler.health(counts, alive=True, ready=True)[0] == "normal"
        scheduler.fault.notify(LoginExpired())
        assert scheduler.health(counts, alive=False, ready=False)[0] == "needs_login"
    finally:
        await store.close()


@pytest.mark.parametrize(
    "health,alive,ready,stale,code",
    [
        ("normal", True, True, False, 0),
        ("normal", True, True, True, 1),
        ("starting", True, True, False, 1),
        ("degraded", True, True, False, 1),
        ("needs_login", False, False, False, 1),
        ("stopped", False, False, False, 1),
    ],
)
def test_status_check_cli_exit_codes(
    tmp_path, monkeypatch, capsys, health, alive, ready, stale, code
):
    import time

    from bili_comment_bot.__main__ import main

    config = tmp_path / "config.toml"
    config.write_text("data_dir = " + json.dumps(str(tmp_path)) + "\n")
    write_private(
        tmp_path / "status-sim.json",
        {
            "mode": "sim",
            "updated_at": time.time() - (100 if stale else 0),
            "stale_after": 30,
            "alive": alive,
            "ready": ready,
            "business_health": health,
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["bili-comment-bot", "--config", str(config), "status", "--namespace", "sim", "--check"]
    )
    if code:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == code
    else:
        main()
    assert json.loads(capsys.readouterr().out)["healthy"] == (code == 0)


def test_status_missing_check_nonzero_and_generic_error_not_called_login(
    tmp_path, monkeypatch, capsys
):
    from bili_comment_bot.__main__ import main

    config = tmp_path / "config.toml"
    config.write_text("data_dir = " + json.dumps(str(tmp_path)) + "\n")
    monkeypatch.setattr(
        "sys.argv", ["bili-comment-bot", "--config", str(config), "status", "--namespace", "sim", "--check"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code != 0 and "登录操作" not in capsys.readouterr().err


async def test_action_backoff_cannot_be_bypassed_by_workflow_or_direct_atomic_claim(tmp_path):
    from bili_comment_bot.domain import Mention

    async with business(tmp_path) as (service, store, fixture, server, _, now):
        await store.put_workflow("discovery:1", flow_payload(1))
        action = PublishAction(
            id="discovery:1:invite",
            kind=ActionKind.INVITE,
            aid=1,
            text="这段日常很有趣",
            mentions=[Mention(uid=123, name="朋友")],
            input_decision=Decision.ALLOW,
            output_safe=True,
            evidence_usable=True,
        )
        await store.put_actions([action])
        await store.defer_work("action:" + action.id)
        assert not (await store.claim_action(action.id)).claimed
        assert await service.dispatcher.execute(action) == ActionStatus.PENDING
        assert await store.due_workflows(1) == []
        assert not fixture.calls and not server.posts
        now[0] += 30
        assert await store.due_workflows(1) == [1]
        assert await service.discover_video(1) == [ActionStatus.SUCCEEDED]
        assert len(server.posts) == 1 and not fixture.calls
        assert (await store.counts())["work_retries"] == 0
