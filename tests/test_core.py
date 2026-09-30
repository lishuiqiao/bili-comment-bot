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
        assert not await second.reserve_quota("new", 10, Channel.DM, 5)
    finally:
        await second.close()


async def test_two_connections_cannot_take_last_slot(tmp_path):
    path = tmp_path / "race.db"
    stores = [await Store(path, clock=lambda: 10000).open() for _ in range(2)]
    try:
        results = await asyncio.gather(
            *(s.reserve_quota(f"race:{i}", 10, Channel.DM, 1) for i, s in enumerate(stores))
        )
        assert sorted(results) == [False, True]
    finally:
        for instance in stores:
            await instance.close()


async def test_pending_reservation_does_not_expire_without_proof(tmp_path):
    now = [10000]
    store = await Store(tmp_path / "reservation.db", clock=lambda: now[0]).open()
    try:
        assert await store.reserve_quota("old", 10, Channel.DM, 1)
        now[0] += 7200
        assert not await store.reserve_quota("new", 10, Channel.DM, 1)
        await store.release_quota("old")
        assert await store.reserve_quota("new", 10, Channel.DM, 1)
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
    await store.release_quota("timeout")
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
    await first.reserve_quota(item.id, item.uid, item.channel, 5)
    assert await first.claim_action(item.id)
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
    await store.resolve_uncertain(actions[failed_step].id, "verified-remote-id", "平台记录已核对")
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
        assert await live.reserve_quota("reply:1", 10, Channel.DM, 1)
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
