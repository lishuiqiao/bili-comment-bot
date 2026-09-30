"""Evaluator harness tests use synthetic model replies, never claim model quality."""

import asyncio
import json
import sys
import time

import httpx
import pytest
from ai_fixtures import FixtureAI, ai_settings, completion

from bili_comment_bot import healthcheck
from bili_comment_bot.ai.client import AIClient, AIError
from bili_comment_bot.config import Settings
from bili_comment_bot.evaluation import EvalCase, evaluate, load_cases, supplied_evidence
from bili_comment_bot.observability import write_private
from bili_comment_bot.safety import SafetyService


async def test_offline_has_no_model_config_calls_or_data_directory_access(tmp_path):
    settings = Settings(data_dir=tmp_path / "must-not-exist")
    report = await evaluate(settings)
    assert report["mode"] == "offline" and report["model"] is None
    assert report["http_requests"] == 0 and not settings.data_dir.exists()
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
    assert report["http_requests"] == len(fixture.calls)
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
    assert report["http_requests"] == len(fixture.calls) == 2
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
    "safe,category", [(False, "unknown"), (True, "unknown"), (False, "allowed"), (True, "harmful")]
)
async def test_unknown_and_inconsistent_output_verdicts_never_pass_or_approve_generation(
    safe, category
):
    fixture = FixtureAI()

    def handler(request):
        payload = json.loads(request.content)
        if "purpose=output_safety" in payload["messages"][0]["content"]:
            return completion({"safe": safe, "category": category})
        return fixture(request)

    report = await evaluate(ai_settings(), mode="real", transport=httpx.MockTransport(handler))
    # Both expected=allow and expected=reject must preserve unknown, not classify it as reject.
    for key in ["output-safe", "output-false-fact"]:
        row = next(row for row in report["results"] if row["id"] == key)
        assert row["status"] == row["actual"] == "unknown"
    assert report["counts"]["human_required"] == 0
    assert all(row["status"] == "unknown" for row in report["results"] if row["kind"] == "generate")
    client = AIClient(ai_settings(), httpx.MockTransport(handler))
    try:
        assert not await SafetyService(ai_settings(), client).check_output("今天慢慢聊吧")
    finally:
        await client.close()


async def test_timeout_preserves_started_trace_and_zero_trace_for_waiting_case():
    cancelled = asyncio.Event()

    async def blocked(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    report = await evaluate(
        ai_settings(),
        mode="real",
        max_cases=2,
        concurrency=1,
        time_budget=0.1,
        transport=httpx.MockTransport(blocked),
    )
    assert report["timed_out"] and cancelled.is_set()
    started, waiting = report["results"]
    assert started["completion_attempts"] == started["http_requests"] == 1
    assert waiting["completion_attempts"] == waiting["http_requests"] == 0
    assert report["completion_attempts"] == report["http_requests"] == 1
    assert all(
        row["status"] == "not_evaluated" and row["errors"] == [] for row in report["results"]
    )
    assert sum(row["completion_attempts"] for row in report["results"]) == 1
    assert sum(row["http_requests"] for row in report["results"]) == 1


@pytest.mark.parametrize("failure", ["input_budget", "request_rate"])
async def test_pre_http_rejection_separates_completion_attempts_from_http_requests(failure):
    overrides = (
        {"max_input_chars": 1000} if failure == "input_budget" else {"max_calls_per_minute": 1}
    )
    fixture = FixtureAI()
    report = await evaluate(
        ai_settings(ai=overrides), mode="real", max_cases=2, transport=httpx.MockTransport(fixture)
    )
    assert report["completion_attempts"] == 2
    assert report["http_requests"] == len(fixture.calls) == (0 if failure == "input_budget" else 1)
    assert report["counts"]["unknown"] > 0
    for field in ["completion_attempts", "http_requests"]:
        assert sum(row[field] for row in report["results"]) == report[field]


def test_eval_cli_unknown_is_nonzero_without_platform_access(tmp_path, monkeypatch, capsys):
    from bili_comment_bot import __main__, evaluation

    config = tmp_path / "config.toml"
    config.write_text('[ai]\nmodel="fixture-model"\nbase_url="https://model.example/v1"\n')
    monkeypatch.setenv("BILI_BOT_AI_API_KEY", "fixture-key")
    fixture = FixtureAI()
    fixture.input_decision = "unknown"
    monkeypatch.setattr(
        evaluation,
        "AIClient",
        lambda settings, transport=None: AIClient(settings, httpx.MockTransport(fixture)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bili-comment-bot",
            "--config",
            str(config),
            "evaluate",
            "--eval-mode",
            "real",
            "--max-cases",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as failed:
        __main__.main()
    assert failed.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["unknown"] == 1
    assert report["completion_attempts"] == report["http_requests"] == 1


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
