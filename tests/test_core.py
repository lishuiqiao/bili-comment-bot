import asyncio
import math

import pytest
from pydantic import ValidationError

from bili_comment_bot.adapters.fake import FakePlatform
from bili_comment_bot.config import AIConfig, Settings, load_settings
from bili_comment_bot.dispatch import Dispatcher
from bili_comment_bot.domain import (
    ActionKind,
    ActionStatus,
    Channel,
    Decision,
    DefinitelyNotSent,
    FollowState,
    LikeStateEvidence,
    Mention,
    MessageEvent,
    PublishAction,
    ReplyLocation,
    VideoEvidence,
    VideoScore,
)
from bili_comment_bot.policy import discovery_steps, eligible
from bili_comment_bot.storage import Store


@pytest.fixture
def live_settings():
    return Settings.model_validate(
        {
            "publishing": {"dry_run": False, "publish_enabled": True},
            "discovery": {"invite_uids": [123]},
        }
    )


@pytest.fixture
async def store(tmp_path):
    instance = await Store(tmp_path / "state.db", clock=lambda: 10000).open()
    yield instance
    await instance.close()


def reply(key="reply:1", uid=10, channel=Channel.DM, **overrides):
    payload = dict(
        id=key,
        kind=ActionKind.REPLY,
        uid=uid,
        channel=channel,
        text="今天也陪着你。",
        input_decision=Decision.ALLOW,
        output_safe=True,
    )
    if channel == Channel.COMMENT:
        payload.update(
            aid=1, location=ReplyLocation(aid=1, root=100, parent=200), evidence_usable=True
        )
    payload.update(overrides)
    return PublishAction(**payload)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (60, []),
        (60.000001, [ActionKind.INVITE]),
        (90, [ActionKind.INVITE]),
        (90.000001, [ActionKind.LIKE, ActionKind.ENCOURAGE, ActionKind.INVITE]),
    ],
)
def test_strict_thresholds(score, expected):
    result = VideoScore(heat=score, recommendation=score, absurdity=score, reasons=["a", "b", "c"])
    assert discovery_steps(result) == expected


@pytest.mark.parametrize("invalid", [-1, 101, math.nan, math.inf, -math.inf, True, "95"])
def test_score_rejects_invalid_numbers(invalid):
    with pytest.raises(ValidationError):
        VideoScore(heat=invalid, recommendation=70, absurdity=70, reasons=["a", "b", "c"])


@pytest.mark.parametrize("state", list(FollowState))
def test_comment_does_not_require_following(state):
    event = MessageEvent(
        id="comment:1",
        uid=10,
        channel=Channel.COMMENT,
        text="总结",
        timestamp=1,
        location=ReplyLocation(aid=1, root=100, parent=200),
    )
    assert eligible(event, state)


@pytest.mark.parametrize("state", [FollowState.NO, FollowState.UNKNOWN])
async def test_dm_follower_gate_applies_to_whitelist(state, store, live_settings):
    settings = live_settings.model_copy(
        update={"limits": live_settings.limits.model_copy(update={"whitelist": [10]})}
    )
    platform = FakePlatform(follows=state)
    assert await Dispatcher(settings, store, platform).execute(reply()) == ActionStatus.BLOCKED
    assert not platform.calls


async def test_user_follows_bot_and_mutual_following_are_eligible(store, live_settings):
    # Adapter direction is sender -> bot, not bot -> sender; wire semantics are tested in phase 2.
    platform = FakePlatform(follows=FollowState.YES)
    assert (
        await Dispatcher(live_settings, store, platform).execute(reply()) == ActionStatus.SUCCEEDED
    )


async def test_separate_quota_and_rejection_counts(store, live_settings):
    dispatcher = Dispatcher(live_settings, store, FakePlatform())
    for channel in Channel:
        for number in range(5):
            item = reply(
                f"{channel}:{number}", channel=channel, refusal=True, input_decision=Decision.REJECT
            )
            assert await dispatcher.execute(item) == ActionStatus.SUCCEEDED
        assert (
            await dispatcher.execute(reply(f"{channel}:six", channel=channel))
            == ActionStatus.BLOCKED
        )


async def test_rolling_quota_exact_one_hour_boundary(tmp_path, live_settings):
    now = [10000.0]
    store = await Store(tmp_path / "clock.db", clock=lambda: now[0]).open()
    try:
        dispatcher = Dispatcher(live_settings, store, FakePlatform())
        for number in range(5):
            assert await dispatcher.execute(reply(str(number))) == ActionStatus.SUCCEEDED
        now[0] += 3599.999
        assert await dispatcher.execute(reply("early")) == ActionStatus.BLOCKED
        now[0] += 0.001
        assert await dispatcher.execute(reply("boundary")) == ActionStatus.SUCCEEDED
    finally:
        await store.close()


async def test_quota_survives_restart(tmp_path, live_settings):
    path = tmp_path / "persistent.db"
    first = await Store(path, clock=lambda: 10000).open()
    dispatcher = Dispatcher(live_settings, first, FakePlatform())
    for number in range(5):
        await dispatcher.execute(reply(str(number)))
    await first.close()
    second = await Store(path, clock=lambda: 10000).open()
    try:
        await second.put_actions([reply("new")])
        assert (await second.claim_action("new")).status == ActionStatus.BLOCKED
    finally:
        await second.close()


async def test_two_connections_cannot_take_last_slot(tmp_path):
    path = tmp_path / "race.db"
    stores = [await Store(path, clock=lambda: 10000).open() for _ in range(2)]
    try:
        await stores[0].put_actions([reply(f"race:{i}") for i in range(2)])
        results = await asyncio.gather(
            *(s.claim_action(f"race:{i}", dm_limit=1) for i, s in enumerate(stores))
        )
        assert sorted(result.claimed for result in results) == [False, True]
    finally:
        for instance in stores:
            await instance.close()


async def test_in_flight_reservation_does_not_expire_without_proof(tmp_path):
    now = [10000]
    store = await Store(tmp_path / "reservation.db", clock=lambda: now[0]).open()
    try:
        await store.put_actions([reply("old"), reply("new"), reply("later")])
        assert (await store.claim_action("old", dm_limit=1)).claimed
        now[0] += 7200
        assert (await store.claim_action("new", dm_limit=1)).status == ActionStatus.BLOCKED
        assert not await store.cancel_pending("old")
        await store.finish_action("old", ActionStatus.FAILED, reason="confirmed not sent")
        assert (await store.claim_action("later", dm_limit=1)).claimed
    finally:
        await store.close()


async def test_whitelist_does_not_bypass_safety(store, live_settings):
    settings = live_settings.model_copy(
        update={
            "limits": live_settings.limits.model_copy(update={"whitelist": [10], "dm_per_hour": 0})
        }
    )
    platform = FakePlatform()
    dispatcher = Dispatcher(settings, store, platform)
    assert await dispatcher.execute(reply("unsafe", output_safe=False)) == ActionStatus.BLOCKED
    assert await dispatcher.execute(reply("safe")) == ActionStatus.SUCCEEDED
    assert len(platform.calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_decision": Decision.UNKNOWN},
        {"output_safe": False},
        {"input_decision": Decision.REJECT},
        {"text": "去找@别人"},
        {"text": "＠别人"},
        {"text": ""},
        {"text": "a" * 801},
    ],
)
async def test_output_and_input_gates(overrides, store, live_settings):
    platform = FakePlatform()
    assert (
        await Dispatcher(live_settings, store, platform).execute(reply(**overrides))
        == ActionStatus.BLOCKED
    )
    assert not platform.calls


async def test_title_only_cannot_be_video_summary(store, live_settings):
    evidence = VideoEvidence(aid=1, bvid="BV1test", title="标题")
    assert not evidence.usable
    platform = FakePlatform(follows=FollowState.NO)
    dispatcher = Dispatcher(live_settings, store, platform)
    assert (
        await dispatcher.execute(reply(channel=Channel.COMMENT, evidence_usable=evidence.usable))
        == ActionStatus.BLOCKED
    )
    assert (
        await dispatcher.execute(
            reply(
                "refusal",
                channel=Channel.COMMENT,
                evidence_usable=False,
                refusal=True,
                input_decision=Decision.REJECT,
            )
        )
        == ActionStatus.SUCCEEDED
    )


async def test_cursor_and_inbox_are_atomic_and_idempotent(store):
    event = MessageEvent(id="dm:1", uid=10, channel=Channel.DM, text="你好", timestamp=1)
    await store.enqueue_batch([event, event], "dm", "1")
    assert len(await store.pending_events()) == 1

    def broken_batch():
        yield event.model_copy(update={"id": "dm:2"})
        raise RuntimeError("collector interrupted before batch completed")

    with pytest.raises(RuntimeError):
        await store.enqueue_batch(broken_batch(), "dm", "2")
    assert await store.cursor("dm") == "1"
    assert len(await store.pending_events()) == 1
    assert await store.claim_event(event.id)
    assert not await store.claim_event(event.id)
    await store.recover()
    assert len(await store.pending_events()) == 1


async def test_success_is_not_repeated(store, live_settings):
    platform = FakePlatform()
    dispatcher = Dispatcher(live_settings, store, platform)
    item = reply()
    assert await dispatcher.execute(item) == ActionStatus.SUCCEEDED
    assert (
        await dispatcher.execute(item.model_copy(update={"text": "新文本"}))
        == ActionStatus.SUCCEEDED
    )
    assert len(platform.calls) == 1
    assert platform.calls[0].text == item.text


async def test_definitely_not_sent_releases_slot(store, live_settings):
    settings = live_settings.model_copy(
        update={"limits": live_settings.limits.model_copy(update={"dm_per_hour": 1})}
    )
    dispatcher = Dispatcher(settings, store, FakePlatform({"fail": DefinitelyNotSent()}))
    assert await dispatcher.execute(reply("fail")) == ActionStatus.FAILED
    assert await dispatcher.execute(reply("new")) == ActionStatus.SUCCEEDED


async def test_timeout_freezes_and_keeps_reservation(store, live_settings):
    settings = live_settings.model_copy(
        update={"limits": live_settings.limits.model_copy(update={"dm_per_hour": 1})}
    )
    platform = FakePlatform({"timeout": TimeoutError()})
    dispatcher = Dispatcher(settings, store, platform)
    assert await dispatcher.execute(reply("timeout")) == ActionStatus.UNCERTAIN
    assert not await store.cancel_pending("timeout")
    assert await dispatcher.execute(reply("new")) == ActionStatus.BLOCKED
    await store.recover()
    assert await dispatcher.execute(reply("timeout")) == ActionStatus.UNCERTAIN
    assert len(platform.calls) == 1


async def test_crash_after_remote_success_before_local_ack(tmp_path, live_settings):
    path = tmp_path / "crash.db"
    first = await Store(path).open()
    item = reply()
    platform = FakePlatform()
    await first.put_actions([item])
    assert (await first.claim_action(item.id)).claimed
    await platform.publish(item)
    await first.close()  # No receipt persisted: an injected process crash.
    second = await Store(path).open()
    try:
        await second.recover()
        assert (
            await Dispatcher(live_settings, second, platform).execute(item)
            == ActionStatus.UNCERTAIN
        )
        assert len(platform.calls) == 1
    finally:
        await second.close()


def discovery_actions():
    previous = None
    actions = []
    for kind in [ActionKind.LIKE, ActionKind.ENCOURAGE, ActionKind.INVITE]:
        item = PublishAction(
            id=f"video:1:{kind}",
            kind=kind,
            aid=1,
            dependency=previous,
            text="" if kind == ActionKind.LIKE else "快来看这段精彩的日常。",
            mentions=[Mention(uid=123, name="朋友")] if kind == ActionKind.INVITE else [],
            input_decision=Decision.ALLOW,
            output_safe=True,
            evidence_usable=True,
        )
        actions.append(item)
        previous = item.id
    return actions


@pytest.mark.parametrize("failed_step", [0, 1, 2])
async def test_high_score_stops_at_each_uncertain_step(failed_step, store, live_settings):
    await store.bind_account(42)
    actions = discovery_actions()
    platform = FakePlatform({actions[failed_step].id: TimeoutError()})
    dispatcher = Dispatcher(live_settings, store, platform)
    for action in actions:
        await dispatcher.execute(action)
    assert [a.kind for a in platform.calls] == [a.kind for a in actions[: failed_step + 1]]
    await store.recover()
    for action in actions:
        await dispatcher.execute(action)
    assert len(platform.calls) == failed_step + 1
    with pytest.raises(ValueError):
        await store.resolve_uncertain(actions[failed_step].id, "", "")
    if failed_step == 0:
        await store.resolve_uncertain(
            actions[0].id,
            None,
            "核对目标状态：此账号已点赞，不断言原超时请求成功",
            like_state=LikeStateEvidence(account_uid=42, aid=1, liked=True),
        )
        assert (await store.action(actions[0].id))["remote_id"] is None
    else:
        await store.resolve_uncertain(actions[failed_step].id, "900001", "平台记录已核对")
    for action in actions:
        await dispatcher.execute(action)
    assert [a.kind for a in platform.calls] == [a.kind for a in actions]


@pytest.mark.parametrize(
    "publishing",
    [
        {"dry_run": True, "publish_enabled": True},
        {"dry_run": False, "publish_enabled": False},
    ],
)
async def test_simulation_isolated_from_live(tmp_path, publishing):
    path = tmp_path / "modes.db"
    settings = Settings.model_validate(
        {"publishing": publishing, "discovery": {"invite_uids": [123]}}
    )
    simulated = await Store(path, namespace="sim").open()
    live = await Store(path, namespace="live").open()
    try:
        platform = FakePlatform()
        dispatcher = Dispatcher(settings, simulated, platform)
        for action in discovery_actions():
            assert await dispatcher.execute(action) == ActionStatus.SIMULATED
        assert await dispatcher.execute(reply()) == ActionStatus.SIMULATED
        assert not platform.calls
        assert await live.action("reply:1") is None
        await live.put_actions([reply()])
        assert (await live.claim_action("reply:1", dm_limit=1)).claimed
        with pytest.raises(ValueError):
            Dispatcher(settings, live, platform)
    finally:
        await simulated.close()
        await live.close()


def test_config_secret_repr_and_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setenv("BILI_BOT_AI_API_KEY", "private-test-key")
    file = tmp_path / "config.toml"
    file.write_text('[persona]\nname="bili-comment-bot"\n')
    settings = load_settings(file)
    assert "private-test-key" not in repr(settings)
    assert settings.ai.api_key.get_secret_value() == "private-test-key"
    assert "private-test-key" not in AIConfig(api_key="private-test-key").model_dump_json()
    with pytest.raises(ValidationError):
        Settings.model_validate({"limits": {"dm_per_hour": -1}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"persona": {"name": "@wrong"}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"discovery": {"invite_uids": [0]}})


async def quota_rows(store):
    async with store.transaction() as db:
        return await (await db.execute("SELECT * FROM quota WHERE ns=?", (store.ns,))).fetchall()


async def test_publish_disabled_after_dispatcher_creation(store, live_settings):
    platform = FakePlatform()
    dispatcher = Dispatcher(live_settings, store, platform)
    live_settings.publishing.publish_enabled = False
    assert await dispatcher.execute(reply()) == ActionStatus.PENDING
    assert not platform.calls
    assert not await quota_rows(store)
    live_settings.publishing.publish_enabled = True
    assert await dispatcher.execute(reply()) == ActionStatus.SUCCEEDED


@pytest.mark.parametrize("switch", ["publish_enabled", "dry_run"])
async def test_publish_switch_changed_while_follow_check_waits(switch, store, live_settings):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingFollower(FakePlatform):
        async def sender_follows_bot(self, uid):
            entered.set()
            await release.wait()
            return FollowState.YES

    platform = WaitingFollower()
    task = asyncio.create_task(Dispatcher(live_settings, store, platform).execute(reply()))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            setattr(live_settings.publishing, switch, switch == "dry_run")
            release.set()
            assert await task == ActionStatus.PENDING
        assert not platform.calls
        assert not await quota_rows(store)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_sim_dispatcher_cannot_become_live_by_mutating_config(tmp_path):
    settings = Settings()
    store = await Store(tmp_path / "sim.db", namespace="sim").open()
    platform = FakePlatform()
    dispatcher = Dispatcher(settings, store, platform)
    try:
        settings.publishing.dry_run = False
        settings.publishing.publish_enabled = True
        assert await dispatcher.execute(reply()) == ActionStatus.PENDING
        assert not await quota_rows(store)
        settings.publishing.dry_run = True
        assert await dispatcher.execute(reply()) == ActionStatus.SIMULATED
        assert not platform.calls
    finally:
        await store.close()


async def test_disable_after_atomic_claim_is_confirmed_unsent(store, live_settings, monkeypatch):
    original = store.claim_action

    async def disable_after_claim(*args, **kwargs):
        result = await original(*args, **kwargs)
        live_settings.publishing.publish_enabled = False
        return result

    monkeypatch.setattr(store, "claim_action", disable_after_claim)
    platform = FakePlatform()
    dispatcher = Dispatcher(live_settings, store, platform)
    assert await dispatcher.execute(reply()) == ActionStatus.FAILED
    assert not platform.calls
    assert (await quota_rows(store))[0]["state"] == "released"
    monkeypatch.setattr(store, "claim_action", original)
    live_settings.publishing.publish_enabled = True
    live_settings.limits.dm_per_hour = 1
    assert await dispatcher.execute(reply("later")) == ActionStatus.SUCCEEDED


async def test_cancellation_cannot_release_in_flight_or_uncertain_slot(store, live_settings):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingWrite(FakePlatform):
        async def publish(self, action):
            self.calls.append(action)
            entered.set()
            await release.wait()
            raise TimeoutError()

    live_settings.limits.dm_per_hour = 1
    platform = WaitingWrite()
    dispatcher = Dispatcher(live_settings, store, platform)
    task = asyncio.create_task(dispatcher.execute(reply()))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            assert not await store.cancel_pending("reply:1")
            assert await dispatcher.execute(reply("second")) == ActionStatus.BLOCKED
            assert (await quota_rows(store))[0]["state"] == "reserved"
            release.set()
            assert await task == ActionStatus.UNCERTAIN
        assert not await store.cancel_pending("reply:1")
        assert (await quota_rows(store))[0]["state"] == "uncertain"
        assert len(platform.calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_pending_cancel_and_claim_are_atomic_across_connections(tmp_path):
    stores = [await Store(tmp_path / "cancel.db").open() for _ in range(2)]
    try:
        await stores[0].put_actions([reply()])
        claim, cancelled = await asyncio.gather(
            stores[0].claim_action("reply:1", dm_limit=1),
            stores[1].cancel_pending("reply:1"),
        )
        assert claim.claimed != cancelled
        row = await stores[0].action("reply:1")
        if cancelled:
            assert claim.status == ActionStatus.BLOCKED
            assert row["status"] == ActionStatus.BLOCKED
            assert not await quota_rows(stores[0])
        else:
            assert claim.status == ActionStatus.IN_FLIGHT
            assert row["status"] == ActionStatus.IN_FLIGHT
            assert len(await quota_rows(stores[0])) == 1
        with pytest.raises(ValueError):
            await stores[0].finish_action("reply:1", ActionStatus.BLOCKED)
    finally:
        for instance in stores:
            await instance.close()


async def test_duplicate_dispatchers_claim_once_and_return_actual_state(tmp_path, live_settings):
    stores = [await Store(tmp_path / "duplicate.db").open() for _ in range(2)]
    both_arrived, followers_release = asyncio.Event(), asyncio.Event()
    write_entered, write_release = asyncio.Event(), asyncio.Event()

    class BarrierPlatform(FakePlatform):
        arrivals = 0

        async def sender_follows_bot(self, uid):
            self.arrivals += 1
            if self.arrivals == 2:
                both_arrived.set()
            await followers_release.wait()
            return FollowState.YES

        async def publish(self, action):
            self.calls.append(action)
            write_entered.set()
            await write_release.wait()
            return await FakePlatform().publish(action)

    platform = BarrierPlatform()
    tasks = [
        asyncio.create_task(Dispatcher(live_settings, s, platform).execute(reply())) for s in stores
    ]
    try:
        async with asyncio.timeout(10):
            await both_arrived.wait()
            followers_release.set()
            await write_entered.wait()
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            assert len(done) == len(pending) == 1
            assert next(iter(done)).result() == ActionStatus.IN_FLIGHT
            assert len(platform.calls) == len(await quota_rows(stores[0])) == 1
            write_release.set()
            assert await next(iter(pending)) == ActionStatus.SUCCEEDED
        assert not await stores[1].cancel_pending("reply:1")
        assert await Dispatcher(live_settings, stores[1], platform).execute(reply()) == (
            ActionStatus.SUCCEEDED
        )
    finally:
        followers_release.set()
        write_release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for instance in stores:
            await instance.close()


async def test_late_follow_success_cannot_reserve_blocked_action(tmp_path, live_settings):
    stores = [await Store(tmp_path / "blocked.db").open() for _ in range(2)]
    entered, release = asyncio.Event(), asyncio.Event()

    class LateSuccess(FakePlatform):
        async def sender_follows_bot(self, uid):
            entered.set()
            await release.wait()
            return FollowState.YES

    good = LateSuccess()
    live_settings.limits.dm_per_hour = 1
    task = asyncio.create_task(Dispatcher(live_settings, stores[0], good).execute(reply()))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            bad = Dispatcher(live_settings, stores[1], FakePlatform(follows=FollowState.NO))
            assert await bad.execute(reply()) == ActionStatus.BLOCKED
            release.set()
            assert await task == ActionStatus.BLOCKED
        assert not good.calls
        assert not await quota_rows(stores[0])
        assert await Dispatcher(live_settings, stores[0], good).execute(reply("new")) == (
            ActionStatus.SUCCEEDED
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for instance in stores:
            await instance.close()


async def test_late_block_cannot_overwrite_claim_or_success(store):
    await store.put_actions([reply()])
    assert (await store.claim_action("reply:1")).claimed
    assert await store.block_pending("reply:1", "late follower result") == ActionStatus.IN_FLIGHT
    assert (await quota_rows(store))[0]["state"] == "reserved"
    await store.finish_action("reply:1", ActionStatus.SUCCEEDED, remote_id="confirmed")
    assert await store.block_pending("reply:1", "late filter result") == ActionStatus.SUCCEEDED
    assert not await store.cancel_pending("reply:1")


@pytest.mark.parametrize("uncertain", [False, True])
async def test_dependency_waits_without_quota_then_claims_after_proof(
    uncertain, store, live_settings
):
    parent = discovery_actions()[0]
    item = reply(dependency=parent.id)
    platform = FakePlatform({parent.id: TimeoutError()} if uncertain else {})
    dispatcher = Dispatcher(live_settings, store, platform)
    assert await dispatcher.execute(item) == ActionStatus.PENDING
    assert not await quota_rows(store)
    status = await dispatcher.execute(parent)
    if uncertain:
        assert status == ActionStatus.UNCERTAIN
        assert await dispatcher.execute(item) == ActionStatus.PENDING
        assert not await quota_rows(store)
        await store.bind_account(42)
        await store.resolve_uncertain(
            parent.id,
            None,
            "verified target is liked",
            like_state=LikeStateEvidence(account_uid=42, aid=1, liked=True),
        )
    assert await dispatcher.execute(item) == ActionStatus.SUCCEEDED
    assert len(await quota_rows(store)) == 1
    assert [action.id for action in platform.calls] == [parent.id, item.id]


async def test_pending_cancellation_preserves_identity_and_never_claims(store):
    await store.put_actions([reply(uid=22, channel=Channel.COMMENT)])
    assert await store.cancel_pending("reply:1")
    result = await store.claim_action("reply:1", dm_limit=1, comment_limit=1)
    assert not result.claimed
    assert result.status == ActionStatus.BLOCKED
    assert not await quota_rows(store)


async def test_quota_uses_persisted_recipient_and_channel(store):
    await store.put_actions([reply(uid=22, channel=Channel.COMMENT)])
    assert (await store.claim_action("reply:1", dm_limit=0, comment_limit=1)).claimed
    row = (await quota_rows(store))[0]
    assert (row["uid"], row["channel"]) == (22, Channel.COMMENT)


@pytest.mark.parametrize(
    "proof,remote_id,note",
    [
        (LikeStateEvidence(account_uid=43, aid=1, liked=True), None, "verified"),
        (LikeStateEvidence(account_uid=42, aid=2, liked=True), None, "verified"),
        (LikeStateEvidence(account_uid=42, aid=1, liked=False), None, "verified"),
        (LikeStateEvidence(account_uid=42, aid=1, liked=True), None, " "),
        (LikeStateEvidence(account_uid=42, aid=1, liked=True), "999", "verified"),
        (None, None, "verified"),
    ],
)
async def test_like_recovery_rejects_mismatched_or_unconfirmed_proof(
    proof, remote_id, note, store, live_settings
):
    await store.bind_account(42)
    item = discovery_actions()[0]
    platform = FakePlatform({item.id: TimeoutError()})
    dispatcher = Dispatcher(live_settings, store, platform)
    assert await dispatcher.execute(item) == ActionStatus.UNCERTAIN
    async with store.transaction() as db:
        before = (await (await db.execute("SELECT COUNT(*) FROM audit")).fetchone())[0]
    with pytest.raises(ValueError):
        await store.resolve_uncertain(item.id, remote_id, note, like_state=proof)
    assert (await store.action(item.id))["status"] == ActionStatus.UNCERTAIN
    async with store.transaction() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM audit")).fetchone())[0] == before
    assert len(platform.calls) == 1


async def test_like_recovery_keeps_null_id_and_atomic_state_audit(store, live_settings):
    import json

    await store.bind_account(42)
    item = discovery_actions()[0]
    dispatcher = Dispatcher(live_settings, store, FakePlatform({item.id: TimeoutError()}))
    assert await dispatcher.execute(item) == ActionStatus.UNCERTAIN
    proof = LikeStateEvidence(account_uid=42, aid=1, liked=True)
    await store.resolve_uncertain(item.id, None, "核对已点赞目标状态", like_state=proof)
    row = await store.action(item.id)
    assert row["status"] == ActionStatus.SUCCEEDED and row["remote_id"] is None
    async with store.transaction() as db:
        audit = await (await db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT 1")).fetchone()
    detail = json.loads(audit["reason"])
    assert detail["verification"] == "manual_target_state_check"
    assert (detail["account_uid"], detail["aid"], detail["liked"]) == (42, 1, True)
    assert audit["reason"] == row["reason"]
    with pytest.raises(ValueError):
        await store.resolve_uncertain(item.id, None, "again", like_state=proof)


@pytest.mark.parametrize("remote_id", [None, "", " ", "0", "fake-id", "-1", True])
@pytest.mark.parametrize("channel", list(Channel))
async def test_comment_and_dm_recovery_require_valid_remote_id(
    remote_id, channel, store, live_settings
):
    item = reply(channel=channel)
    platform = FakePlatform({item.id: TimeoutError()})
    assert await Dispatcher(live_settings, store, platform).execute(item) == ActionStatus.UNCERTAIN
    with pytest.raises(ValueError):
        await store.resolve_uncertain(item.id, remote_id, "checked")
    assert (await store.action(item.id))["status"] == ActionStatus.UNCERTAIN
    await store.resolve_uncertain(item.id, "900002", "verified actual resource")
    assert (await store.action(item.id))["remote_id"] == "900002"
    assert (await quota_rows(store))[0]["state"] == "sent"


async def test_unbound_account_cannot_verify_uncertain_like(store, live_settings):
    item = discovery_actions()[0]
    assert (
        await Dispatcher(live_settings, store, FakePlatform({item.id: TimeoutError()})).execute(
            item
        )
        == ActionStatus.UNCERTAIN
    )
    with pytest.raises(ValueError):
        await store.resolve_uncertain(
            item.id,
            None,
            "checked",
            like_state=LikeStateEvidence(account_uid=42, aid=1, liked=True),
        )
