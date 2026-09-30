"""Synthetic completions prove orchestration/contracts, not real model judgement."""

import json
import re

import httpx

from bili_comment_bot.config import Settings
from bili_comment_bot.domain import VideoEvidence


def ai_settings(*, live=False, **overrides):
    raw = {
        "ai": {
            "base_url": "https://model.example/v1",
            "model": "fixture-model",
            "api_key": "fixture-key",
        },
        "publishing": {"publish_enabled": live, "dry_run": not live},
        "discovery": {"invite_uids": [123]},
    }
    for key, value in overrides.items():
        raw.setdefault(key, {}).update(value)
    return Settings.model_validate(raw)


def completion(data, *, finish="stop", **message_fields):
    content = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": content, **message_fields},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def video_evidence(**overrides):
    raw = dict(
        aid=1,
        bvid="BV1234567890",
        title="温暖日常",
        description="散步与陪伴",
        transcript="P1：早晨散步，晚上看星星。",
        sources=["bilibili-subtitle:aid=1:cid=1:language=zh-CN"],
        scope_cids=[1],
        languages=["zh-CN"],
        complete=True,
        status="complete",
        duration=30,
        published_at=10000,
        snapshot_at=10000,
        content_acquired_at=10000,
        stats=dict(view=1000000, like=200000, coin=0, favorite=0, reply=0, share=0),
        comments=["好看", "有趣", "很抽象"],
        comment_sample={"sort": "hot", "limitation": "biased sample"},
    )
    raw.update(overrides)
    return VideoEvidence(**raw)


class FixtureAI:
    def __init__(self):
        self.calls = []
        self.text_overrides = {}
        self.fail_purposes = set()
        self.input_decision = "allow"
        self.output_safe = True
        self.recommendation = 99.0
        self.absurdity = 99.0
        self.false_citation = False

    def __call__(self, request):
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-key"
        payload = json.loads(request.content)
        assert [message["role"] for message in payload["messages"]] == ["system", "user"]
        prompt = payload["messages"][0]["content"]
        purpose = re.search(r"purpose=(\w+)", prompt).group(1)
        data = json.loads(payload["messages"][1]["content"])
        self.calls.append((purpose, data, payload))
        if purpose in self.fail_purposes:
            raise httpx.ReadTimeout("private message fixture-key", request=request)
        if purpose in {"input_safety", "source_safety"}:
            decision = self.input_decision
            category = {"allow": "allowed", "reject": "unrelated", "unknown": "unknown"}[decision]
            if data.get("stage") == "video_relevance" and "无关" in data.get("message", ""):
                decision, category = "reject", "unrelated"
            return completion({"decision": decision, "category": category})
        if purpose == "output_safety":
            return completion(
                {"safe": self.output_safe, "category": "allowed" if self.output_safe else "harmful"}
            )
        sources = (data.get("video_evidence") or {}).get("sources", [])
        if self.false_citation:
            sources = ["invented-source"]
        if purpose == "rating":
            return completion(
                {
                    "recommendation": self.recommendation,
                    "absurdity": self.absurdity,
                    "recommendation_reason": "温暖日常，样本积极",
                    "absurdity_reason": "出人意料的笑点",
                    "citations": sources,
                }
            )
        return completion(
            {
                "text": self.text_overrides.get(purpose, "今天也陪着你。这段日常很有趣。"),
                "citations": sources,
            }
        )

    @property
    def purposes(self):
        return [item[0] for item in self.calls]
