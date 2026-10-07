"""OpenAI-compatible JSON completions; no recovery of malformed model prose."""

import asyncio
import json
import logging
import time
from collections import deque

import httpx
from pydantic import ValidationError

from ..config import Settings


class AIError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        self.reason, self.status = reason, status
        super().__init__(f"AI request failed: {reason}; status={status}")


class AIClient:
    def __init__(self, settings: Settings, transport=None, local_runner=None):
        config = settings.ai
        if not settings.ai_ready:
            raise AIError("configuration_missing")
        if (
            config.backend == "api"
            and config.base_url.startswith("http:")
            and not config.allow_insecure_http
        ):
            raise AIError("insecure_endpoint_disabled")
        self.local_runner = local_runner
        if config.backend == "local_mlx" and self.local_runner is None:
            from .local import LocalRunner

            self.local_runner = LocalRunner(settings)
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.limits.concurrency)
        self.calls = deque()
        self.metrics = {"requests": 0, "failures": 0, "prompt_tokens": 0, "completion_tokens": 0}
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def complete(self, system: str, data: dict, contract):
        config = self.settings.ai
        schema = contract.model_json_schema()
        payload = {
            "model": config.model,
            "messages": [
                {
                    "role": "system",
                    "content": system
                    + "\n只输出 JSON，严格符合以下契约："
                    + json.dumps(schema, ensure_ascii=False),
                },
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            config.token_parameter: config.max_tokens,
            "stream": False,
        }
        if sum(len(message["content"]) for message in payload["messages"]) > config.max_input_chars:
            raise AIError("input_budget_exceeded")
        if config.send_temperature:
            payload["temperature"] = config.temperature
        if config.structured_output == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif config.structured_output == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": contract.__name__.lower(),
                    "strict": True,
                    "schema": schema,
                },
            }
        try:
            async with asyncio.timeout(None if config.backend == "local_mlx" else config.timeout):
                async with self.semaphore:
                    for attempt in range(config.retries + 1):
                        now = time.monotonic()
                        while self.calls and self.calls[0] <= now - 60:
                            self.calls.popleft()
                        if len(self.calls) >= config.max_calls_per_minute:
                            raise AIError("request_budget_exhausted")
                        self.calls.append(now)
                        self.metrics["requests"] += 1
                        try:
                            envelope = await self._request(payload)
                        except AIError as error:
                            if error.status not in {429, 503} or attempt == config.retries:
                                raise
                            await asyncio.sleep(0.5 * 2**attempt)
                            continue
                        choices = envelope.get("choices") if isinstance(envelope, dict) else None
                        if not isinstance(choices, list) or len(choices) != 1:
                            raise AIError("invalid_envelope")
                        choice = choices[0]
                        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                            raise AIError("incomplete_completion")
                        message = choice.get("message")
                        if (
                            not isinstance(message, dict)
                            or message.get("role") != "assistant"
                            or message.get("tool_calls")
                            or message.get("function_call")
                            or message.get("refusal")
                            or not isinstance(message.get("content"), str)
                            or not message["content"].strip()
                        ):
                            raise AIError("invalid_completion")
                        try:
                            result = contract.model_validate_json(message["content"])
                        except ValidationError:
                            raise AIError("contract_violation") from None
                        usage = envelope.get("usage", {})
                        if isinstance(usage, dict):
                            for key in ("prompt_tokens", "completion_tokens"):
                                amount = usage.get(key)
                                if type(amount) is int and 0 <= amount <= 1_000_000:
                                    self.metrics[key] += amount
                        return result
                    raise AIError("retry_budget_exhausted")
        except (httpx.HTTPError, TimeoutError):
            self.metrics["failures"] += 1
            raise AIError("network_or_timeout") from None
        except AIError:
            self.metrics["failures"] += 1
            raise

    async def _request(self, payload):
        config = self.settings.ai
        if config.backend == "local_mlx":
            content = await self.local_runner.run(
                "complete",
                {
                    "messages": payload["messages"],
                    "max_tokens": config.max_tokens,
                    "temperature": config.temperature if config.send_temperature else 0,
                },
            )
            if not isinstance(content, str) or len(content.encode()) > config.max_response_bytes:
                raise AIError("invalid_local_completion")
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                    }
                ]
            }
        async with self.client.stream(
            "POST",
            config.base_url + "/chat/completions",
            json=payload,
            headers={
                "Authorization": "Bearer " + config.api_key.get_secret_value(),
                "Accept-Encoding": "identity",
            },
        ) as response:
            if response.status_code != 200:
                raise AIError("http_error", response.status_code)
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > config.max_response_bytes:
                    raise AIError("response_too_large")
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                raise AIError("invalid_json") from None
