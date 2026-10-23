"""The provider adapters against scripted mock providers: streaming, tool calls and their round
trips, every error mapping, usage, timeouts and cancellation. See docs/31-chat-and-adapters.md.
No network; test values, not secrets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from gateway.adapters import AnthropicAdapter, OllamaAdapter, OpenAICompatibleAdapter
from gateway.adapters.base import HttpAdapter
from gateway.adapters.http import make_client
from gateway.chat import (
    ChatDelta,
    ChatInputError,
    ChatMessage,
    ChatRequest,
    NamedTool,
    ProviderError,
    ProviderErrorCode,
    ToolCall,
    ToolSpec,
)
from gateway.chatservice import ChatService
from gateway.clock import SimClock
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest
from gateway.secret import Secret
from mock_providers import (
    Reply,
    ScriptedProvider,
    anthropic_events,
    ndjson,
    ollama_text,
    ollama_tools,
    openai_text,
    openai_tools,
    sse,
)

KEY = "sk-test-0123456789abcdefghijklmnopqrstuvwxyz"
WEATHER = ToolSpec(
    "get_weather",
    "Current weather for a city",
    {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)

Factory = Callable[[ScriptedProvider], HttpAdapter]


def ollama(provider: ScriptedProvider) -> HttpAdapter:
    return OllamaAdapter(
        name="economy",
        base_url="http://127.0.0.1:11434",
        model="llama3.2:1b",
        client=provider.client(),
    )


def openai(provider: ScriptedProvider) -> HttpAdapter:
    return OpenAICompatibleAdapter(
        name="hosted",
        base_url="https://api.example.test/v1",
        model="model-x",
        client=provider.client(),
    )


def anthropic(provider: ScriptedProvider, **kwargs: Any) -> HttpAdapter:
    return AnthropicAdapter(
        name="claude",
        base_url="https://api.anthropic.test",
        model="claude-test",
        client=provider.client(),
        **kwargs,
    )


def text_reply(factory: Factory, *pieces: str) -> Reply:
    if factory is ollama:
        return ollama_text(*pieces)
    if factory is openai:
        return openai_text(*pieces)
    return anthropic_events(*({"text": p} for p in pieces))


ADAPTERS = [ollama, openai, anthropic]
IDS = ["ollama", "openai_compatible", "anthropic"]


def ask(text: str = "What is the weather in Paris?", **kwargs: Any) -> ChatRequest:
    return ChatRequest("req-1", "economy", (ChatMessage("user", text),), **kwargs)


async def run(
    adapter: HttpAdapter, request: ChatRequest, credential: Secret | None = None
) -> tuple[Any, list[ChatDelta]]:
    deltas: list[ChatDelta] = []
    result = await adapter.chat(request, credential=credential, on_delta=deltas.append)
    return result, deltas


# --- text, usage and the request each sends -------------------------------------------------------


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
async def test_text_streams_in_order_with_the_providers_usage(factory: Factory) -> None:
    provider = ScriptedProvider(SimClock(), [text_reply(factory, "Hello", " there", "!")])
    result, deltas = await run(factory(provider), ask(), Secret(KEY))
    assert [d.text for d in deltas] == ["Hello", " there", "!"]
    assert result.content == "Hello there!"
    assert result.finish_reason == "stop"
    assert not result.usage.estimated
    assert result.usage.output_tokens > 0


async def test_each_adapter_sends_its_own_format_and_credential_header() -> None:
    clock = SimClock()
    sent = {}
    for factory in ADAPTERS:
        provider = ScriptedProvider(clock, [text_reply(factory, "ok")])
        await run(factory(provider), ask(max_tokens=5, temperature=0.5), Secret(KEY))
        sent[factory.__name__] = provider.requests[0]
    o, a, c = sent["ollama"], sent["openai"], sent["anthropic"]
    assert o.url == "http://127.0.0.1:11434/api/chat"
    assert o.body["options"] == {"temperature": 0.5, "num_predict": 5}
    assert "authorization" not in o.headers  # Ollama: the operator's, takes no key
    assert a.url == "https://api.example.test/v1/chat/completions"
    assert a.headers["authorization"] == f"Bearer {KEY}"
    assert a.body["stream_options"] == {"include_usage": True}
    assert c.url == "https://api.anthropic.test/v1/messages"
    assert c.headers["x-api-key"] == KEY
    assert c.headers["anthropic-version"] == "2023-06-01"
    assert c.body["max_tokens"] == 5
    assert "thinking" not in c.body  # sent only when configured


def _without_usage(data: bytes) -> bytes:
    data = data.replace(b'"usage": {"input_tokens": 25, "output_tokens": 1}', b'"usage": {}')
    return data.replace(b', "usage": {"output_tokens": 15}', b"")


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
async def test_missing_usage_is_estimated_and_marked(factory: Factory) -> None:
    if factory is ollama:
        done = {"message": {"content": ""}, "done": True, "done_reason": "stop"}
        reply = Reply(chunks=ndjson({"message": {"content": "Hello there"}}, done))
    elif factory is openai:
        reply = openai_text("Hello there", usage=False)
    else:
        reply = anthropic_events({"text": "Hello there"})
        reply.chunks = [(delay, _without_usage(data)) for delay, data in reply.chunks]
    provider = ScriptedProvider(SimClock(), [reply])
    result, _ = await run(factory(provider), ask("Say hello, please"))
    assert result.usage.estimated
    assert result.usage.output_tokens == 3  # "Hello there": 11 characters, one per four
    assert result.usage.input_tokens >= 1


async def test_anthropic_system_messages_merging_and_limits() -> None:
    provider = ScriptedProvider(SimClock(), [anthropic_events({"text": "ok"})])
    adapter = anthropic(provider, default_max_tokens=77, thinking={"type": "disabled"})
    request = ChatRequest(
        "r",
        "premium",
        (
            ChatMessage("system", "Be brief."),
            ChatMessage("user", "One."),
            ChatMessage("user", "Two."),
        ),
        temperature=1.7,
    )
    await run(adapter, request, Secret(KEY))
    body = provider.requests[0].body
    assert body["system"] == "Be brief."
    assert body["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "One."}, {"type": "text", "text": "Two."}],
        }
    ]
    assert body["max_tokens"] == 77
    assert body["temperature"] == 1.0
    assert body["thinking"] == {"type": "disabled"}


# --- tool calls ----------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
async def test_a_tool_call_round_trip(factory: Factory) -> None:
    clock = SimClock()
    if factory is ollama:
        first = ollama_tools(("get_weather", {"city": "Paris"}))
    elif factory is openai:
        first = openai_tools(("call_1", "get_weather", ['{"ci', 'ty": "Pa', 'ris"}']))
    else:
        first = anthropic_events(
            {"tool": ("toolu_1", "get_weather", ['{"city":', ' "Paris"}'])}, stop="tool_use"
        )
    provider = ScriptedProvider(clock, [first, text_reply(factory, "It is sunny.")])
    adapter = factory(provider)
    result, deltas = await run(adapter, ask(tools=(WEATHER,)), Secret(KEY))
    assert result.finish_reason == "tool_calls"
    [call] = result.tool_calls
    assert call.name == "get_weather"
    assert json.loads(call.arguments) == {"city": "Paris"}
    assert [d.tool_call for d in deltas if d.tool_call] == [call]

    follow_up = ChatRequest(
        "req-2",
        "economy",
        (
            ChatMessage("user", "What is the weather in Paris?"),
            ChatMessage("assistant", None, (call,)),
            ChatMessage("tool", "18 C and sunny", tool_call_id=call.id),
        ),
        tools=(WEATHER,),
    )
    answer, _ = await run(adapter, follow_up, Secret(KEY))
    assert answer.content == "It is sunny."
    sent = provider.requests[1].body
    if factory is ollama:
        assert sent["messages"][1]["tool_calls"] == [
            {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
        ]
        assert sent["messages"][2] == {
            "role": "tool",
            "tool_name": "get_weather",
            "content": "18 C and sunny",
        }
    elif factory is openai:
        assert sent["messages"][2] == {
            "role": "tool",
            "content": "18 C and sunny",
            "tool_call_id": "call_1",
        }
    else:
        assert sent["messages"][1]["content"] == [
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}
        ]
        assert sent["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "18 C and sunny"}
            ],
        }


async def test_ollama_gives_each_call_its_own_id_and_maps_results_by_position_and_name() -> None:
    """The same tool twice in one turn: two ids; the results, sent back in the other order,
    reach Ollama in the calls' order, each named."""
    provider = ScriptedProvider(
        SimClock(),
        [
            ollama_tools(("get_weather", {"city": "Paris"}), ("get_weather", {"city": "Rome"})),
            ollama_text("Both sunny."),
        ],
    )
    adapter = OllamaAdapter(
        name="economy",
        base_url="http://127.0.0.1:11434",
        model="llama3.2:1b",
        client=provider.client(),
    )
    result, _ = await run(adapter, ask(tools=(WEATHER,)))
    paris, rome = result.tool_calls
    assert paris.id != rome.id
    assert len(paris.id) == len("call_") + 24
    assert (json.loads(paris.arguments), json.loads(rome.arguments)) == (
        {"city": "Paris"},
        {"city": "Rome"},
    )
    follow_up = ChatRequest(
        "req-2",
        "economy",
        (
            ChatMessage("user", "Paris and Rome?"),
            ChatMessage("assistant", None, (paris, rome)),
            ChatMessage("tool", "Rome: 24 C", tool_call_id=rome.id),
            ChatMessage("tool", "Paris: 18 C", tool_call_id=paris.id),
        ),
        tools=(WEATHER,),
    )
    await run(adapter, follow_up)
    sent = provider.requests[1].body["messages"]
    assert sent[2:] == [
        {"role": "tool", "tool_name": "get_weather", "content": "Paris: 18 C"},
        {"role": "tool", "tool_name": "get_weather", "content": "Rome: 24 C"},
    ]
    # A result that answers no call of the assistant message before it is refused.
    stray = ChatRequest(
        "req-3",
        "economy",
        (
            ChatMessage("assistant", None, (paris,)),
            ChatMessage("tool", "x", tool_call_id="call_nope"),
        ),
    )
    with pytest.raises(ChatInputError) as refused:
        await run(adapter, stray)
    assert refused.value.param == "messages[1].tool_call_id"
    assert len(provider.requests) == 2  # nothing was sent for it


async def test_ollama_ids_do_not_repeat_across_turns() -> None:
    replies = [ollama_tools(("get_weather", {"city": "Paris"})) for _ in range(20)]
    provider = ScriptedProvider(SimClock(), replies)
    adapter = OllamaAdapter(
        name="economy", base_url="http://x:1", model="m", client=provider.client()
    )
    ids = set()
    for _ in range(20):
        result, _ = await run(adapter, ask(tools=(WEATHER,)))
        ids.add(result.tool_calls[0].id)
    assert len(ids) == 20


async def test_ollama_cannot_be_told_to_call_a_tool() -> None:
    provider = ScriptedProvider(SimClock())
    adapter = OllamaAdapter(name="e", base_url="http://x:1", model="m", client=provider.client())
    for choice in ("required", NamedTool("get_weather")):
        with pytest.raises(ProviderError) as refused:
            await run(adapter, ask(tools=(WEATHER,), tool_choice=choice))
        assert refused.value.code is ProviderErrorCode.TOOLS_NOT_SUPPORTED
    assert provider.requests == []


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
async def test_tool_arguments_that_are_not_a_json_object_are_malformed(factory: Factory) -> None:
    if factory is ollama:
        bad = Reply(
            chunks=ndjson(
                {"message": {"tool_calls": [{"function": {"name": "f", "arguments": "[1, 2]"}}]}},
                {"done": True},
            )
        )
    elif factory is openai:
        bad = openai_tools(("call_1", "f", ['{"a": ']))
    else:
        bad = anthropic_events({"tool": ("toolu_1", "f", ['{"a": '])}, stop="tool_use")
    provider = ScriptedProvider(SimClock(), [bad])
    with pytest.raises(ProviderError) as failed:
        await run(factory(provider), ask(tools=(WEATHER,)))
    assert failed.value.code is ProviderErrorCode.UPSTREAM_MALFORMED
    assert failed.value.detail == "tool_arguments"


async def test_anthropic_thinking_blocks_are_never_dropped_silently() -> None:
    provider = ScriptedProvider(
        SimClock(),
        [
            anthropic_events({"thinking": True}, {"text": "Hello"}),
            anthropic_events(
                {"thinking": True}, {"tool": ("toolu_1", "get_weather", ["{}"])}, stop="tool_use"
            ),
        ],
    )
    adapter = anthropic(provider)
    result, _ = await run(adapter, ask(), Secret(KEY))
    assert result.content == "Hello"
    assert result.thinking_blocks_omitted == 1  # counted, not hidden
    assert adapter.capabilities().supports_tools is None
    with pytest.raises(ProviderError) as refused:
        await run(adapter, ask(tools=(WEATHER,)), Secret(KEY))
    assert refused.value.code is ProviderErrorCode.TOOLS_NOT_SUPPORTED
    assert refused.value.status == 422
    assert refused.value.detail == "thinking_blocks_required"
    capabilities = adapter.capabilities()
    assert capabilities.supports_tools is False
    assert capabilities.reasons["supports_tools"] == "thinking_blocks_required"


# --- errors --------------------------------------------------------------------------------------


def _status(
    status: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> Reply:
    data = json.dumps(body or {"error": {"type": "invalid_request_error", "message": KEY}})
    return Reply(status=status, chunks=[(0.0, data.encode())], headers=headers or {})


ERRORS: list[tuple[Reply, ProviderErrorCode, int, str | None, int | None]] = [
    (_status(401), ProviderErrorCode.PROVIDER_AUTH_FAILED, 502, None, None),
    (_status(403), ProviderErrorCode.PROVIDER_AUTH_FAILED, 502, None, None),
    (
        _status(429, headers={"Retry-After": "7"}),
        ProviderErrorCode.UPSTREAM_RATE_LIMITED,
        429,
        None,
        7,
    ),
    (
        _status(429, headers={"Retry-After": "soon"}),
        ProviderErrorCode.UPSTREAM_RATE_LIMITED,
        429,
        None,
        None,
    ),
    (
        _status(429, headers={"Retry-After": "99999"}),
        ProviderErrorCode.UPSTREAM_RATE_LIMITED,
        429,
        None,
        None,
    ),
    (_status(500), ProviderErrorCode.UPSTREAM_UNAVAILABLE, 502, "server_error", None),
    (_status(503), ProviderErrorCode.UPSTREAM_UNAVAILABLE, 502, "server_error", None),
    (_status(529), ProviderErrorCode.UPSTREAM_UNAVAILABLE, 502, "server_error", None),
    (_status(400), ProviderErrorCode.UPSTREAM_REJECTED, 502, "invalid_request_error", None),
    (
        _status(400, {"error": {"type": "made_up", "message": "x"}}),
        ProviderErrorCode.UPSTREAM_REJECTED,
        502,
        None,
        None,
    ),
    (
        _status(302, headers={"Location": "http://169.254.169.254/"}),
        ProviderErrorCode.UPSTREAM_REJECTED,
        502,
        "redirect",
        None,
    ),
    (
        Reply(raise_error=httpx.ConnectError("refused")),
        ProviderErrorCode.UPSTREAM_UNAVAILABLE,
        502,
        "connect",
        None,
    ),
    (
        Reply(raise_error=httpx.ConnectTimeout("slow")),
        ProviderErrorCode.UPSTREAM_TIMEOUT,
        502,
        "connect",
        None,
    ),
]


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
@pytest.mark.parametrize(
    ("reply", "code", "status", "detail", "retry_after"),
    ERRORS,
    ids=[f"{r.status}-{r.headers}-{type(r.raise_error).__name__}" for r, *_ in ERRORS],
)
async def test_every_upstream_failure_is_mapped_and_never_echoed(
    factory: Factory,
    reply: Reply,
    code: ProviderErrorCode,
    status: int,
    detail: str | None,
    retry_after: int | None,
) -> None:
    provider = ScriptedProvider(SimClock(), [reply])
    with pytest.raises(ProviderError) as failed:
        await run(factory(provider), ask(), Secret(KEY))
    error = failed.value
    assert (error.code, error.status, error.detail, error.retry_after_s) == (
        code,
        status,
        detail,
        retry_after,
    )
    # The provider's body (which here echoes the key) is never part of the error.
    for text in (str(error), repr(error), error.message, repr(error.__cause__)):
        assert KEY not in text


@pytest.mark.parametrize("factory", ADAPTERS, ids=IDS)
async def test_a_malformed_or_broken_stream_is_mapped(factory: Factory) -> None:
    clock = SimClock()
    garbage = Reply(chunks=[(5.0, b"data: {not json\n\n" if factory is not ollama else b"{nope\n")])
    early = text_reply(factory, "Hello", " world")
    early.chunks = early.chunks[:2]  # the end never comes
    broken = text_reply(factory, "Hello", " there")
    broken.fail_after = 2
    provider = ScriptedProvider(clock, [garbage, early, broken])
    adapter = factory(provider)
    outcomes = []
    for _ in range(3):
        with pytest.raises(ProviderError) as failed:
            await run(adapter, ask())
        outcomes.append((failed.value.code, failed.value.detail))
    assert outcomes == [
        (ProviderErrorCode.UPSTREAM_MALFORMED, "not_json"),
        (ProviderErrorCode.UPSTREAM_MALFORMED, "ended_early"),
        (ProviderErrorCode.UPSTREAM_UNAVAILABLE, "connection_lost"),
    ]


async def test_anthropic_errors_inside_the_stream_are_mapped() -> None:
    expected = {
        "overloaded_error": ProviderErrorCode.UPSTREAM_UNAVAILABLE,
        "rate_limit_error": ProviderErrorCode.UPSTREAM_RATE_LIMITED,
        "authentication_error": ProviderErrorCode.PROVIDER_AUTH_FAILED,
        "something_new": ProviderErrorCode.UPSTREAM_MALFORMED,
    }
    for kind, code in expected.items():
        reply = Reply(
            chunks=sse(
                {"event": "message_start", "type": "message_start", "message": {"usage": {}}},
                {"event": "error", "type": "error", "error": {"type": kind, "message": KEY}},
            )
        )
        provider = ScriptedProvider(SimClock(), [reply])
        with pytest.raises(ProviderError) as failed:
            await run(anthropic(provider), ask(), Secret(KEY))
        assert failed.value.code is code
        assert KEY not in str(failed.value)


# --- timeouts and cancellation -----------------------------------------------------------------


def _service(clock: SimClock, adapter: HttpAdapter, **kwargs: float) -> ChatService:
    return ChatService(ModelRegistry(), clock, {"economy": adapter}, **kwargs)


async def test_the_first_token_timeout_cancels_the_upstream_request() -> None:
    clock = SimClock()
    slow = openai_text("late")
    slow.chunks = [(5_000.0, data) for _, data in slow.chunks]
    provider = ScriptedProvider(clock, [slow])
    service = _service(clock, openai(provider), first_token_timeout_ms=1_000.0)
    with pytest.raises(ProviderError) as failed:
        await service.run(ask())
    assert (failed.value.code, failed.value.detail) == (
        ProviderErrorCode.UPSTREAM_TIMEOUT,
        "first_token",
    )
    assert clock.now_ms() == 1_000.0
    assert provider.streams[0].closed


async def test_the_total_timeout_cancels_a_slow_answer() -> None:
    clock = SimClock()
    dribble = openai_text(*["x"] * 50)
    dribble.chunks = [(100.0, data) for _, data in dribble.chunks]
    provider = ScriptedProvider(clock, [dribble])
    service = _service(
        clock, openai(provider), first_token_timeout_ms=1_000.0, total_timeout_ms=2_000.0
    )
    with pytest.raises(ProviderError) as failed:
        await service.run(ask())
    assert (failed.value.code, failed.value.detail) == (ProviderErrorCode.UPSTREAM_TIMEOUT, "total")
    assert clock.now_ms() == 2_000.0
    assert provider.streams[0].closed


async def test_cancelling_the_request_closes_the_upstream_stream() -> None:
    """What happens when the client goes away: the request's task is cancelled."""
    clock = SimClock()
    long = openai_text(*["x"] * 100)
    provider = ScriptedProvider(clock, [long])
    service = _service(clock, openai(provider))
    seen: list[ChatDelta] = []
    task = asyncio.ensure_future(service.run(ask(), seen.append))
    while len(seen) < 3:
        await clock.sleep_ms(1.0)  # lets simulated time, and the stream, move on
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.streams[0].closed
    assert provider.streams[0].sent < 10


async def test_timing_and_usage_of_a_served_answer() -> None:
    clock = SimClock()
    provider = ScriptedProvider(clock, [openai_text("a", "b")])
    outcome = await _service(clock, openai(provider)).run(ask())
    timing = outcome.timing
    assert timing.first_token_ms == 20.0  # the role chunk at 10 ms carries no text
    assert timing.finish_ms > timing.first_token_ms
    assert (timing.tokens_in, timing.tokens_out) == (9, 2)
    assert outcome.provider == "local"


# --- the rest -------------------------------------------------------------------------------------


async def test_generate_is_one_chat_turn() -> None:
    clock = SimClock()
    provider = ScriptedProvider(clock, [ollama_text("Hi", "!")])
    pieces: list[tuple[int, str]] = []
    generation = await ollama(provider).generate(
        GenerateRequest("r", "economy", "Say hi", 4), on_token=lambda i, t: pieces.append((i, t))
    )
    assert generation.text == "Hi!"
    assert pieces == [(0, "Hi"), (1, "!")]
    assert provider.requests[0].body["messages"] == [{"role": "user", "content": "Say hi"}]


def test_the_client_takes_no_proxy_and_follows_no_redirect() -> None:
    client = make_client()
    assert client.follow_redirects is False
    assert client._trust_env is False


def test_adapters_hold_no_credential() -> None:
    clock = SimClock()
    for factory in ADAPTERS:
        adapter = factory(ScriptedProvider(clock))
        assert not any(isinstance(v, Secret) for v in vars(adapter).values())
        assert KEY not in repr(adapter)


def test_tool_call_arguments_must_be_an_object() -> None:
    assert ToolCall("c", "f", '{"a": 1}').parsed_arguments() == {"a": 1}
    with pytest.raises(ValueError, match="not a JSON object"):
        ToolCall("c", "f", "[1]").parsed_arguments()
