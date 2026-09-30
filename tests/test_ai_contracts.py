import asyncio

import httpx
import pytest
from ai_fixtures import FixtureAI, ai_settings, completion, video_evidence
from pydantic import ValidationError

from bili_comment_bot.ai.client import AIClient, AIError
from bili_comment_bot.ai.contracts import ContentRating, GeneratedText
from bili_comment_bot.ai.prompts import PROMPT_VERSION, PURPOSES
from bili_comment_bot.ai.service import AIService
from bili_comment_bot.config import AIConfig


@pytest.mark.parametrize("mode", ["json_object", "json_schema", "prompt"])
async def test_request_structured_modes_token_budget_and_persona_temperature_separation(mode):
    fixture = FixtureAI()
    settings = ai_settings(
        ai={"structured_output": mode, "temperature": 0.1},
        persona={"name": "小团", "warmth": 0.95, "humor": 0.3, "empathy": 0.9},
    )
    client = AIClient(settings, httpx.MockTransport(fixture))
    ai = AIService(settings, client)
    try:
        for purpose in sorted(PURPOSES):
            evidence = video_evidence() if purpose in {"summary", "encourage", "invite"} else None
            output = await ai.generate(purpose, evidence=evidence, reason="out_of_scope")
            assert output
        assert set(fixture.purposes) == PURPOSES
        for _, _, payload in fixture.calls:
            prompt = payload["messages"][0]["content"]
            assert PROMPT_VERSION in prompt and '"name": "小团"' in prompt
            assert '"warmth": 0.95' in prompt and payload["temperature"] == 0.1
            assert payload["max_completion_tokens"] == settings.ai.max_tokens
            assert not payload["stream"] and "tools" not in payload
            if mode == "prompt":
                assert "response_format" not in payload
            else:
                assert payload["response_format"]["type"] == mode
        assert client.metrics["requests"] == 6 and client.metrics["prompt_tokens"] == 600
    finally:
        await client.close()


@pytest.mark.parametrize(
    "outcome",
    [
        "malformed",
        "unknown_field",
        "empty",
        "truncated",
        "tool",
        "refusal",
        "bad_role",
        "envelope",
        "extra_choice",
    ],
)
async def test_invalid_completions_never_recover_a_success(outcome, caplog):
    def handler(r):
        data = {"text": "安全", "citations": []}
        if outcome == "malformed":
            return completion('before {"text":"safe","citations":[]} after')
        if outcome == "unknown_field":
            data["uid"] = 123
        if outcome == "empty":
            return completion(" ")
        if outcome == "truncated":
            return completion(data, finish="length")
        if outcome == "tool":
            return completion(data, tool_calls=[{"id": "do_not_execute"}])
        if outcome == "refusal":
            return completion(data, refusal="cannot")
        if outcome == "bad_role":
            return completion(data, role="system")
        if outcome == "envelope":
            return httpx.Response(200, json={"choices": None})
        if outcome == "extra_choice":
            return httpx.Response(200, json={"choices": [{}, {}]})
        return completion(data)

    client = AIClient(ai_settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(AIError) as caught:
            await client.complete("只输出 JSON", {"message": "private"}, GeneratedText)
        assert "private" not in str(caught.value) and "fixture-key" not in caplog.text
    finally:
        await client.close()


@pytest.mark.parametrize("number", [101, -1, "95", True])
async def test_rating_contract_rejects_non_numeric_or_out_of_bounds(number):
    client = AIClient(
        ai_settings(),
        httpx.MockTransport(
            lambda r: completion(
                {
                    "recommendation": number,
                    "absurdity": 50.0,
                    "recommendation_reason": "x",
                    "absurdity_reason": "x",
                    "citations": ["source"],
                }
            )
        ),
    )
    try:
        with pytest.raises(AIError):
            await client.complete("JSON", {}, ContentRating)
    finally:
        await client.close()


async def test_nan_is_rejected_even_if_provider_returns_nonstandard_json():
    content = (
        '{"recommendation":NaN,"absurdity":50,"recommendation_reason":"x",'
        '"absurdity_reason":"x","citations":["source"]}'
    )
    client = AIClient(ai_settings(), httpx.MockTransport(lambda r: completion(content)))
    try:
        with pytest.raises(AIError):
            await client.complete("JSON", {}, ContentRating)
    finally:
        await client.close()


async def test_total_timeout_and_response_byte_limits():
    entered = asyncio.Event()

    async def hanging(r):
        entered.set()
        await asyncio.Event().wait()

    client = AIClient(ai_settings(ai={"timeout": 0.02}), httpx.MockTransport(hanging))
    try:
        with pytest.raises(AIError, match="network_or_timeout"):
            await client.complete("JSON", {}, GeneratedText)
        assert entered.is_set()
    finally:
        await client.close()
    client = AIClient(
        ai_settings(ai={"max_response_bytes": 1000}),
        httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 1001)),
    )
    try:
        with pytest.raises(AIError, match="response_too_large"):
            await client.complete("JSON", {}, GeneratedText)
    finally:
        await client.close()


async def test_explicit_retry_and_request_budget(monkeypatch):
    calls, delays = [], []

    def handler(r):
        calls.append(r)
        return (
            httpx.Response(429) if len(calls) == 1 else completion({"text": "ok", "citations": []})
        )

    async def wait(delay):
        delays.append(delay)

    monkeypatch.setattr("bili_comment_bot.ai.client.asyncio.sleep", wait)
    client = AIClient(
        ai_settings(ai={"retries": 1, "max_calls_per_minute": 2}), httpx.MockTransport(handler)
    )
    try:
        assert (await client.complete("JSON", {}, GeneratedText)).text == "ok"
        assert len(calls) == 2 and delays == [0.5]
        with pytest.raises(AIError, match="request_budget_exhausted"):
            await client.complete("JSON", {}, GeneratedText)
        assert len(calls) == 2
    finally:
        await client.close()


async def test_redirect_never_forwards_authorization_or_retries():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(302, headers={"location": "https://evil.invalid"})

    client = AIClient(ai_settings(ai={"retries": 2}), httpx.MockTransport(handler))
    try:
        with pytest.raises(AIError):
            await client.complete("JSON", {}, GeneratedText)
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:key@example.com/v1",
        "https://@example.com/v1",
        "https://example.com/v1?key=x",
        "file:///private/a",
    ],
)
def test_ai_base_url_cannot_embed_credentials_or_query(url):
    with pytest.raises(ValidationError):
        AIConfig(base_url=url)


async def test_false_citation_partial_evidence_and_missing_sample_never_qualify():
    fixture = FixtureAI()
    fixture.false_citation = True
    client = AIClient(ai_settings(), httpx.MockTransport(fixture))
    ai = AIService(ai_settings(), client)
    try:
        with pytest.raises(AIError, match="citation"):
            await ai.generate("summary", evidence=video_evidence())
        with pytest.raises(AIError, match="insufficient"):
            await ai.generate("summary", evidence=video_evidence(complete=False))
        with pytest.raises(AIError, match="insufficient"):
            await ai.rate(video_evidence(comments=[]))
    finally:
        await client.close()
