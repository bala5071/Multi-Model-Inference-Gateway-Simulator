"""``POST /v1/chat/completions``: the OpenAI wire format, checked against OpenAI's documented
examples and required fields (tests/fixtures/openai_wire), authentication, refusals by field,
headers, streaming, errors mapped from providers, and a client hanging up. See
docs/31-chat-and-adapters.md. No network; test values, not secrets."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.adapters import OpenAICompatibleAdapter
from gateway.app import create_app
from gateway.backends import Backend
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from helpers import application_access, bearer
from mock_providers import Reply, ScriptedProvider, openai_text, openai_tools

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "openai_wire" / "chat_completions.json").read_text(
        encoding="utf-8"
    )
)
REQUIRED: Mapping[str, list[str]] = FIXTURE["required"]
EXAMPLES: Mapping[str, Any] = FIXTURE["examples"]


@dataclass
class Gateway:
    app: FastAPI
    client: httpx.AsyncClient
    clock: SimClock
    key: str
    provider: ScriptedProvider

    async def chat(self, body: dict[str, Any], key: str | None = None) -> httpx.Response:
        headers = bearer(key if key is not None else self.key)
        return await self.client.post("/v1/chat/completions", json=body, headers=headers)


@asynccontextmanager
async def gateway(
    *, adapter_tier: str | None = None, replies: list[Reply] | None = None
) -> AsyncIterator[Gateway]:
    """The gateway on the simulator, with one application on the enterprise plan (every tier).
    With ``adapter_tier``, that tier is served by an OpenAI-compatible adapter in front of a
    scripted provider."""
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    access, keys = application_access(clock, apps={"acme": "enterprise"})
    provider = ScriptedProvider(clock, list(replies or []))
    adapters: dict[str, Backend] = {}
    if adapter_tier is not None:
        adapters[adapter_tier] = OpenAICompatibleAdapter(
            name=adapter_tier,
            base_url="http://127.0.0.1:8081/v1",
            model="local-model",
            client=provider.client(),
        )
    app = create_app(registry, clock, applications=access, chat_adapters=adapters)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(app, client, clock, keys["acme"], provider)
    await registry.aclose()


def documented(title: str, tier: str = "economy") -> dict[str, Any]:
    """A documented example request, with its model replaced by a tier of this gateway."""
    body: dict[str, Any] = copy.deepcopy(EXAMPLES[title]["request"])
    body["model"] = tier
    return body


def has_required(value: Mapping[str, Any], which: str) -> None:
    missing = set(REQUIRED[which]) - set(value)
    assert not missing, (which, missing, value)


def chunks_of(text: str) -> tuple[list[dict[str, Any]], bool]:
    """The data events of a stream, and whether it ended with [DONE]."""
    events = [block.removeprefix("data: ") for block in text.split("\n\n") if block]
    done = bool(events) and events[-1] == "[DONE]"
    return [json.loads(e) for e in events if e != "[DONE]"], done


# --- the OpenAI wire format, against its documentation ------------------------------------------


async def test_the_documented_default_request_gets_a_documented_answer() -> None:
    async with gateway() as g:
        response = await g.chat(documented("Default"))
    assert response.status_code == 200, response.text
    body = response.json()
    has_required(body, "response")
    assert body["object"] == EXAMPLES["Default"]["response"]["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "economy"
    [choice] = body["choices"]
    has_required(choice, "response_choice")
    has_required(choice["message"], "message")
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].startswith("[simulated output, ")
    assert choice["finish_reason"] in ("stop", "length")
    has_required(body["usage"], "usage")
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    # The documented request's "developer" message was taken as a system message.
    assert response.headers["x-model-used"] == "economy"
    assert response.headers["x-provider"] == "gateway"
    assert response.headers["x-provider-class"] == "local"
    assert "x-cost-usd" not in response.headers


async def test_the_documented_streaming_request_streams_documented_chunks() -> None:
    async with gateway() as g:
        response = await g.chat(
            {**documented("Streaming"), "stream_options": {"include_usage": True}}
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks, done = chunks_of(response.text)
    assert done
    documented_chunk = EXAMPLES["Streaming"]["chunks"][0]
    for chunk in chunks:
        has_required(chunk, "chunk")
        assert chunk["object"] == documented_chunk["object"] == "chat.completion.chunk"
        for choice in chunk["choices"]:
            has_required(choice, "chunk_choice")
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text.startswith("[simulated output, ")
    finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert len(finals) == 1
    assert chunks[-1]["choices"] == []  # the usage chunk, as asked
    has_required(chunks[-1]["usage"], "usage")


async def test_the_documented_functions_request_gets_documented_tool_calls() -> None:
    reply = openai_tools(("call_abc", "get_current_weather", ['{"location": ', '"Boston, MA"}']))
    async with gateway(adapter_tier="standard", replies=[reply]) as g:
        response = await g.chat(documented("Functions", "standard"))
    assert response.status_code == 200, response.text
    body = response.json()
    [choice] = body["choices"]
    assert (
        choice["finish_reason"] == EXAMPLES["Functions"]["response"]["choices"][0]["finish_reason"]
    )
    message = choice["message"]
    has_required(message, "message")
    assert message["content"] is None  # as documented, for an answer that is only tool calls
    [call] = message["tool_calls"]
    has_required(call, "tool_call")
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_current_weather"
    assert json.loads(call["function"]["arguments"]) == {"location": "Boston, MA"}
    # What the adapter sent on: the documented tools and tool_choice.
    sent = g.provider.requests[0].body
    assert sent["tools"] == EXAMPLES["Functions"]["request"]["tools"]
    assert sent["tool_choice"] == "auto"
    assert sent["model"] == "local-model"


async def test_streamed_tool_calls_are_documented_chunks() -> None:
    reply = openai_tools(("call_1", "get_current_weather", ['{"location": "Boston"}']))
    async with gateway(adapter_tier="standard", replies=[reply]) as g:
        response = await g.chat({**documented("Functions", "standard"), "stream": True})
    chunks, done = chunks_of(response.text)
    assert done
    calls = [
        call
        for c in chunks
        if c["choices"]
        for call in c["choices"][0]["delta"].get("tool_calls", [])
    ]
    assert len(calls) == 1
    has_required(calls[0], "chunk_tool_call")
    assert calls[0]["index"] == 0
    assert calls[0]["function"]["name"] == "get_current_weather"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_errors_are_in_the_documented_error_shape() -> None:
    async with gateway() as g:
        refusals = [
            await g.chat(documented("Default"), key="gwk_" + "0" * 16 + "_" + "A" * 43),
            await g.chat({**documented("Default"), "n": 2}),
            await g.chat(documented("Functions")),  # the simulator has no tools
        ]
    for response in refusals:
        body = response.json()
        has_required(body, "error_response")
        has_required(body["error"], "error")
    assert [r.status_code for r in refusals] == [401, 422, 422]
    assert refusals[2].json()["error"]["code"] == "tools_not_supported"
    assert "simulated_model" in refusals[2].json()["error"]["message"]


# --- what is refused, by name ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "param"),
    [
        ({"n": 2}, "n"),
        ({"logprobs": True}, "logprobs"),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"functions": []}, "functions"),
        ({"max_completion_tokens": 5}, "max_completion_tokens"),
        ({"seed": 1}, "seed"),
        ({"user": "someone"}, "user"),
    ],
)
async def test_unsupported_fields_are_refused_by_name(extra: dict[str, Any], param: str) -> None:
    async with gateway() as g:
        response = await g.chat({**documented("Default"), **extra})
    assert response.status_code == 422
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("unsupported_field", param)
    assert param in error["message"]


async def test_other_bad_requests_name_the_field() -> None:
    image = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
    }
    cases: list[tuple[dict[str, Any], str]] = [
        ({"model": "economy", "messages": [image]}, "messages.0.content"),
        ({"model": "economy", "messages": []}, "messages"),
        ({"messages": [{"role": "user", "content": "hi"}]}, "model"),
        (
            {
                "model": "economy",
                "messages": [{"role": "tool", "content": "x", "tool_call_id": "call_9"}],
            },
            "messages[0].tool_call_id",
        ),
        (
            {
                "model": "economy",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
            "tool_choice",
        ),
        (
            {"model": "economy", "messages": [{"role": "user", "content": "hi"}], "temperature": 3},
            "temperature",
        ),
        (
            {
                "model": "economy",
                "messages": [{"role": "user", "content": "hi"}],
                "stream_options": {"include_usage": True},
            },
            "stream_options",
        ),
    ]
    async with gateway() as g:
        for body, param in cases:
            response = await g.chat(body)
            assert response.status_code == 422, (body, response.text)
            assert response.json()["error"]["param"].startswith(param), (param, response.json())


async def test_a_key_is_required_and_checked() -> None:
    async with gateway() as g:
        missing = await g.client.post("/v1/chat/completions", json=documented("Default"))
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "api_key_required"
        assert missing.json()["error"]["type"] == "authentication_error"
        unknown = await model_in_plan(g)
        assert unknown.status_code == 404


async def model_in_plan(g: Gateway) -> httpx.Response:
    return await g.chat({**documented("Default"), "model": "no-such-model"})


# --- providers behind the endpoint ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "status", "code", "retry_after"),
    [
        (
            Reply(status=401, chunks=[(0.0, b'{"error": {"message": "sk-leaked"}}')]),
            502,
            "provider_auth_failed",
            None,
        ),
        (Reply(status=429, headers={"Retry-After": "12"}), 429, "upstream_rate_limited", "12"),
        (Reply(status=503), 502, "upstream_unavailable", None),
        (Reply(chunks=[(0.0, b"data: {broken\n\n")]), 502, "upstream_malformed", None),
    ],
)
async def test_provider_failures_before_any_output_are_mapped(
    reply: Reply, status: int, code: str, retry_after: str | None
) -> None:
    async with gateway(adapter_tier="economy", replies=[reply]) as g:
        for stream in (False, True):
            g.provider.replies.append(copy.deepcopy(reply))
            response = await g.chat({**documented("Default"), "stream": stream})
            assert response.status_code == status, response.text
            assert response.json()["error"]["code"] == code
            assert response.headers.get("retry-after") == retry_after
            assert "sk-leaked" not in response.text


async def test_a_failure_after_output_ends_the_stream_with_an_error_event() -> None:
    broken = openai_text("Hello", " there", " friend")
    broken.fail_after = 3  # the role chunk and two pieces, then the connection drops
    async with gateway(adapter_tier="economy", replies=[broken]) as g:
        response = await g.chat({**documented("Default"), "stream": True})
    assert response.status_code == 200
    chunks, done = chunks_of(response.text)
    assert not done
    assert chunks[-1] == {
        "error": {
            "message": "The model provider is unavailable. (connection_lost)",
            "type": "api_error",
            "param": None,
            "code": "upstream_unavailable",
        }
    }
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if "choices" in c)
    assert text == "Hello there"


async def test_usage_is_charged_and_an_estimate_is_marked() -> None:
    async with gateway(adapter_tier="economy", replies=[openai_text("Hi", usage=False)]) as g:
        response = await g.chat(documented("Default"))
        usage = await g.client.get("/v1/applications/me/usage", headers=bearer(g.key))
    assert response.headers["x-usage-estimated"] == "true"
    assert response.json()["usage"]["usage_estimated"] is True
    charged = usage.json()["by_model"]["economy"]
    assert charged["completion_tokens"] == response.json()["usage"]["completion_tokens"]
    assert charged["requests"] == 1


async def test_the_application_plan_still_decides() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    access, keys = application_access(clock, apps={"basic": "free"}, limits={"basic": (16, None)})
    app = create_app(registry, clock, applications=access)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        not_in_plan = await client.post(
            "/v1/chat/completions",
            json=documented("Default", "premium"),
            headers=bearer(keys["basic"]),
        )
        too_long = await client.post(
            "/v1/chat/completions",
            json={**documented("Default"), "max_tokens": 17},
            headers=bearer(keys["basic"]),
        )
    await registry.aclose()
    assert not_in_plan.status_code == 403
    assert not_in_plan.json()["error"]["code"] == "model_not_in_plan"
    assert too_long.status_code == 422
    assert too_long.json()["error"]["param"] == "max_tokens"


# --- a client hanging up --------------------------------------------------------------------------


async def test_a_client_hanging_up_cancels_the_upstream_request() -> None:
    long = openai_text(*["word "] * 200)
    async with gateway(adapter_tier="economy", replies=[long]) as g:
        body = json.dumps({**documented("Default"), "stream": True}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"gateway"),
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {g.key}".encode()),
            ],
            "client": ("test-client", 1),
            "server": ("gateway", 80),
        }
        disconnected = asyncio.Event()
        sent = False
        chunks: list[bytes] = []

        async def receive() -> MutableMapping[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                if len(chunks) == 4:
                    disconnected.set()

        await g.app(scope, receive, send)
        await g.clock.sleep_ms(1_000.0)
    [stream] = g.provider.streams
    assert stream.closed
    assert stream.sent < 20  # of 203 chunks
