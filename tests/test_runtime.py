"""Production runtime wiring with synthetic HTTP; no real account/media/model claims."""

import asyncio
import json
import logging
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from ai_fixtures import FixtureAI, ai_settings
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
from bili_comment_bot.domain import (
    ActionKind,
    ActionStatus,
    Channel,
    Decision,
    LikeStateEvidence,
    PublishAction,
)
from bili_comment_bot.instance_lock import InstanceInUse, InstanceLock
from bili_comment_bot.observability import log_result, read_status, write_private
from bili_comment_bot.operations import operate
from bili_comment_bot.runtime import RuntimeIO, Scheduler, run_bot
from bili_comment_bot.storage import Store


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
    db.executescript(
        "CREATE TABLE workflows(ns TEXT,id TEXT,payload TEXT,PRIMARY KEY(ns,id));"
        'INSERT INTO workflows VALUES("live","discovery:9",\'{"aid":9}\');'
    )
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
        {"mode": "live", "updated_at": 1, "stale_after": 10, "alive": True, "ready": True},
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
