"""Evaluator harness tests use synthetic model replies, never claim model quality."""

import asyncio
import json
import sys
import time

import httpx
import pytest
from ai_fixtures import FixtureAI, ai_settings

from bili_comment_bot import healthcheck
from bili_comment_bot.ai.client import AIError
from bili_comment_bot.config import Settings
from bili_comment_bot.evaluation import EvalCase, evaluate, load_cases, supplied_evidence
from bili_comment_bot.observability import write_private


async def test_offline_has_no_model_config_calls_or_data_directory_access(tmp_path):
    settings = Settings(data_dir=tmp_path / "must-not-exist")
    report = await evaluate(settings)
    assert report["mode"] == "offline" and report["model"] is None
    assert report["model_calls"] == 0 and not settings.data_dir.exists()
    assert report["counts"]["pass"] > 0
    assert report["counts"]["not_evaluated"] > 0
    assert not report["release_approved"]
    assert report["counts"]["false_allow"] == report["counts"]["false_reject"] == 0
    assert all(
        row["method"] != "real_model_and_rules" for row in report["results"] if "method" in row
    )


async def test_real_missing_model_config_fails_without_fallback_or_auth_files(tmp_path):
    with pytest.raises(AIError):
        await evaluate(Settings(data_dir=tmp_path / "absent"), mode="real")
    assert not (tmp_path / "absent").exists()


async def test_real_harness_has_video_context_mismatch_counts_and_all_persona_paths(tmp_path):
    settings = ai_settings()
    settings.data_dir = tmp_path / "absent"
    fixture = FixtureAI()
    report = await evaluate(settings, mode="real", transport=httpx.MockTransport(fixture))
    assert not settings.data_dir.exists()
    assert report["counts"]["false_allow"] > 0  # Prewritten fixture is no safety oracle.
    assert report["counts"]["human_required"] == 7
    assert report["model_calls"] == len(fixture.calls)
    assert "fixture-key" not in json.dumps(report)
    generated = [row for row in report["results"] if row["status"] == "human_required"]
    assert {row["purpose"] for row in generated} == {
        "companion",
        "summary",
        "refuse_request",
        "insufficient_evidence",
        "encourage",
        "invite",
    }
    mixed = next(row for row in generated if row["id"] == "mixed-spoken-summary")
    assert "P1,P2 字幕与音频转写" in mixed["generated_text"]
    assert all(row["human_ratings"]["persona"] == "pending" for row in generated)
    relevance = [
        data
        for purpose, data, _ in fixture.calls
        if purpose == "input_safety" and data["channel"] == "comment"
    ]
    assert relevance and all(data["video_evidence"]["transcript"] for data in relevance)


async def test_real_budget_bounds_actual_requests_even_with_configured_retries():
    settings = ai_settings(ai={"retries": 2})
    fixture = FixtureAI()
    report = await evaluate(
        settings, mode="real", max_calls=2, concurrency=8, transport=httpx.MockTransport(fixture)
    )
    assert report["model_calls"] == len(fixture.calls) == 2
    assert report["counts"]["unknown"] > 0
    assert any("evaluation_call_budget" in row.get("errors", []) for row in report["results"])


async def test_timeout_and_safety_errors_never_count_as_pass_and_cancel_requests():
    cancelled = asyncio.Event()

    async def blocked(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    report = await evaluate(
        ai_settings(),
        mode="real",
        max_cases=1,
        time_budget=0.1,
        transport=httpx.MockTransport(blocked),
    )
    assert cancelled.is_set() and report["timed_out"]
    assert report["counts"]["pass"] == 0
    assert report["results"][0]["reason"] == "total_timeout"
    fixture = FixtureAI()
    fixture.fail_purposes = {"output_safety"}
    report = await evaluate(ai_settings(), mode="real", transport=httpx.MockTransport(fixture))
    output = next(row for row in report["results"] if row["id"] == "output-safe")
    assert output["status"] == "unknown"
    assert report["counts"]["human_required"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_calls": 0},
        {"max_cases": 101},
        {"concurrency": 9},
        {"time_budget": 0},
        {"mode": "fake"},
    ],
)
async def test_invalid_eval_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        await evaluate(Settings(), **kwargs)


def test_dataset_version_ids_and_comment_evidence_are_valid():
    cases, digest = load_cases()
    assert len(cases) == 26 and len(digest) == 64
    assert supplied_evidence("mixed").usable
    assert not supplied_evidence("insufficient").usable
    with pytest.raises(ValueError):
        EvalCase(id="no-evidence", kind="input", channel="comment")


@pytest.mark.parametrize("live", [False, True])
def test_local_health_probe_follows_config_namespace_and_missing_status_is_one(
    tmp_path, monkeypatch, live
):
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir="{tmp_path}"\n[publishing]\ndry_run={str(not live).lower()}\n'
        f"publish_enabled={str(live).lower()}\n"
    )
    monkeypatch.setattr(sys, "argv", ["healthcheck", str(config)])
    with pytest.raises(SystemExit) as missing:
        healthcheck.main()
    assert missing.value.code == 1
    namespace = "live" if live else "sim"
    write_private(
        tmp_path / f"status-{namespace}.json",
        {
            "mode": namespace,
            "updated_at": time.time(),
            "stale_after": 60,
            "alive": True,
            "ready": True,
            "business_health": "normal",
        },
    )
    with pytest.raises(SystemExit) as good:
        healthcheck.main()
    assert good.value.code == 0
    write_private(
        tmp_path / f"status-{namespace}.json",
        {
            "mode": namespace,
            "updated_at": time.time() - 100,
            "stale_after": 60,
            "alive": True,
            "ready": True,
            "business_health": "normal",
        },
    )
    with pytest.raises(SystemExit) as stale:
        healthcheck.main()
    assert stale.value.code == 1
