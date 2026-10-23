"""Live tests against real providers. **The hosted ones can cost money.**

Skipped unless asked for, and never run by CI (docs/31-chat-and-adapters.md, docs/provider-plan.md
section 9):

- ``live_provider`` (hosted, can cost money): ``GATEWAY_LIVE_PROVIDERS=1`` and, for each
  provider, its own variables, set by whoever runs them in their own environment:
  ``GATEWAY_LIVE_OPENAI_BASE_URL``, ``GATEWAY_LIVE_OPENAI_MODEL``, ``GATEWAY_LIVE_OPENAI_KEY``;
  ``GATEWAY_LIVE_ANTHROPIC_MODEL``, ``GATEWAY_LIVE_ANTHROPIC_KEY``. Each provider gets at most
  four tiny requests (one plain, one tool round trip of two turns), ``max_tokens`` 16.
- ``real_models`` (local Ollama, free): ``GATEWAY_REAL_MODELS=1`` and Ollama on 127.0.0.1:11434
  with ``llama3.2:1b-instruct-q4_K_M``.

Nothing here prints a key; it is read from the environment into a ``Secret``.
"""

from __future__ import annotations

import json
import os

import pytest

from gateway.adapters import AnthropicAdapter, OllamaAdapter, OpenAICompatibleAdapter
from gateway.adapters.base import HttpAdapter
from gateway.chat import ChatMessage, ChatRequest, ToolSpec
from gateway.secret import Secret

PING = ToolSpec("ping", "Answers pong.", {"type": "object", "properties": {}})


def _needs(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"set {', '.join(missing)} to run")
    return {n: os.environ[n] for n in names}


async def _plain_and_tools(adapter: HttpAdapter, credential: Secret | None) -> None:
    ask = ChatRequest(
        "live-1", "live", (ChatMessage("user", "Reply with the word OK."),), max_tokens=16
    )
    plain = await adapter.chat(ask, credential=credential)
    assert plain.content
    assert plain.usage.output_tokens >= 1
    call_it = ChatRequest(
        "live-2",
        "live",
        (ChatMessage("user", "Call the ping tool."),),
        tools=(PING,),
        max_tokens=16,
    )
    called = await adapter.chat(call_it, credential=credential)
    if not called.tool_calls:
        pytest.skip("the model answered without calling the tool")
    [call] = called.tool_calls
    assert call.name == "ping"
    assert isinstance(json.loads(call.arguments), dict)
    answer = await adapter.chat(
        ChatRequest(
            "live-3",
            "live",
            (
                ChatMessage("user", "Call the ping tool."),
                ChatMessage("assistant", None, (call,)),
                ChatMessage("tool", "pong", tool_call_id=call.id),
            ),
            tools=(PING,),
            max_tokens=16,
        ),
        credential=credential,
    )
    assert answer.finish_reason in ("stop", "length")
    await adapter.aclose()


@pytest.mark.live_provider
async def test_live_openai_compatible() -> None:
    env = _needs(
        "GATEWAY_LIVE_OPENAI_BASE_URL", "GATEWAY_LIVE_OPENAI_MODEL", "GATEWAY_LIVE_OPENAI_KEY"
    )
    adapter = OpenAICompatibleAdapter(
        name="live",
        base_url=env["GATEWAY_LIVE_OPENAI_BASE_URL"],
        model=env["GATEWAY_LIVE_OPENAI_MODEL"],
    )
    await _plain_and_tools(adapter, Secret(env["GATEWAY_LIVE_OPENAI_KEY"]))


@pytest.mark.live_provider
async def test_live_anthropic() -> None:
    env = _needs("GATEWAY_LIVE_ANTHROPIC_MODEL", "GATEWAY_LIVE_ANTHROPIC_KEY")
    adapter = AnthropicAdapter(
        name="live", base_url="https://api.anthropic.com", model=env["GATEWAY_LIVE_ANTHROPIC_MODEL"]
    )
    await _plain_and_tools(adapter, Secret(env["GATEWAY_LIVE_ANTHROPIC_KEY"]))


@pytest.mark.real_models
async def test_local_ollama_tool_round_trip() -> None:
    adapter = OllamaAdapter(
        name="economy", base_url="http://127.0.0.1:11434", model="llama3.2:1b-instruct-q4_K_M"
    )
    await _plain_and_tools(adapter, None)
