"""The one chat format every provider adapter speaks, and the errors they all map to.

See docs/31-chat-and-adapters.md and docs/provider-plan.md, section 2.

- Messages are ``system``, ``user``, ``assistant`` (with any tool calls it made) and ``tool``
  (a tool's result, naming the call it answers). Content is text only.
- A tool is a name, a description and a JSON Schema object for its arguments; a tool call is an
  id, a name and its arguments as JSON text, always a JSON object.
- A streamed answer is a series of ``ChatDelta``: text as it comes, and each tool call whole,
  once its arguments parse. The end is a ``ChatResult`` with the usage, taken from the provider,
  or estimated and marked so.
- Every way a provider can fail is a ``ProviderError`` with a fixed code and message. A
  provider's own error text is never passed on: providers echo parts of a bad key in it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls"]
# What OpenAI and Anthropic both accept as a tool's name.
TOOL_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    # JSON text of an object, as the OpenAI format carries it.
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        parsed = json_object(self.arguments)
        if parsed is None:
            raise ValueError(f"the arguments of tool call {self.id!r} are not a JSON object")
        return parsed


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    # On a tool message: the id of the call it answers.
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    # A JSON Schema with "type": "object".
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NamedTool:
    """``tool_choice`` naming one tool the model must call."""

    name: str


ToolChoice = Literal["auto", "none", "required"] | NamedTool


@dataclass(frozen=True, slots=True)
class ChatRequest:
    request_id: str
    # The tier the request was routed to; an adapter sends its own configured model.
    model: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice = "auto"
    max_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a chat request needs at least one message")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class ChatDelta:
    """A piece of an answer: some text, or one complete tool call."""

    text: str = ""
    tool_call: ToolCall | None = None


ChatDeltaCallback = Callable[[ChatDelta], None]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    # True when the provider reported none and the gateway estimated it.
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: Usage
    # Anthropic thinking blocks in an answer without a tool call: not part of the OpenAI
    # format, so not shown, and counted here so they never disappear unannounced.
    thinking_blocks_omitted: int = 0


class Source(StrEnum):
    """Where a capability flag came from."""

    DECLARED = "declared"  # the provider says so (Ollama's /api/show)
    PROBED = "probed"  # test-connection saw it
    CONFIGURED = "configured"  # the operator or organization entered it
    BUILT_IN = "built_in"  # the gateway knows it (the simulator)
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a configured model can do. ``None`` is unknown, never false or zero."""

    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    context_window: int | None = None
    sources: Mapping[str, Source] = field(default_factory=dict)
    # Why a flag is false, when there is a reason worth telling (for example a model that
    # needs its thinking blocks sent back).
    reasons: Mapping[str, str] = field(default_factory=dict)


CAPABILITY_FLAGS = ("supports_tools", "supports_streaming", "context_window")
UNKNOWN_CAPABILITIES = Capabilities(sources=dict.fromkeys(CAPABILITY_FLAGS, Source.UNKNOWN))


class ProviderErrorCode(StrEnum):
    PROVIDER_AUTH_FAILED = "provider_auth_failed"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_MALFORMED = "upstream_malformed"
    UPSTREAM_REJECTED = "upstream_rejected"
    TOOLS_NOT_SUPPORTED = "tools_not_supported"
    # The provider's address is not one the gateway may connect to (docs/32).
    EGRESS_REFUSED = "egress_refused"


# The answer the gateway gives for each, and its fixed message. Nothing a provider wrote.
_ERRORS: Mapping[ProviderErrorCode, tuple[int, str]] = {
    ProviderErrorCode.PROVIDER_AUTH_FAILED: (
        502,
        "The model provider refused the gateway's credentials. An administrator must check "
        "the provider's key.",
    ),
    ProviderErrorCode.UPSTREAM_RATE_LIMITED: (429, "The model provider is rate limiting."),
    ProviderErrorCode.UPSTREAM_UNAVAILABLE: (502, "The model provider is unavailable."),
    ProviderErrorCode.UPSTREAM_TIMEOUT: (502, "The model provider did not answer in time."),
    ProviderErrorCode.UPSTREAM_MALFORMED: (502, "The model provider sent a malformed answer."),
    ProviderErrorCode.UPSTREAM_REJECTED: (502, "The model provider rejected the request."),
    ProviderErrorCode.TOOLS_NOT_SUPPORTED: (422, "This model cannot be used with tools here."),
    ProviderErrorCode.EGRESS_REFUSED: (
        502,
        "The provider's address is not one the gateway may connect to.",
    ),
}


class ProviderError(Exception):
    """A provider failed, mapped. ``detail`` is one of a fixed set of words (which timeout,
    which provider error type, why tools are refused), never provider text."""

    def __init__(
        self,
        code: ProviderErrorCode,
        *,
        detail: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        self.code = code
        self.status, self.message = _ERRORS[code]
        self.detail = detail
        self.retry_after_s = retry_after_s
        super().__init__(f"{code.value}" + (f" ({detail})" if detail else ""))


class ChatInputError(ValueError):
    """A chat request the gateway cannot send as asked (answered 422). ``param`` names the
    field."""

    def __init__(self, message: str, param: str | None = None) -> None:
        self.param = param
        super().__init__(message)


class ChatNotSupportedError(Exception):
    """A backend that does not speak the chat format."""


def json_object(text: str) -> dict[str, Any] | None:
    """``text`` parsed if it is a JSON object, else None."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def estimate_tokens(text: str) -> int:
    """A rough count for when a provider reports no usage: one token per four characters.
    Only ever used together with ``Usage.estimated``."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


def estimate_input_tokens(request: ChatRequest) -> int:
    parts = [m.content or "" for m in request.messages]
    parts += [c.arguments for m in request.messages for c in m.tool_calls]
    parts += [json.dumps(dict(t.parameters)) + t.description for t in request.tools]
    return max(1, estimate_tokens("".join(parts)))


def flatten_messages(messages: tuple[ChatMessage, ...]) -> str:
    """The conversation as one prompt, for a backend that takes only a prompt (the
    simulator)."""
    lines = []
    for message in messages:
        text = message.content or ""
        if message.tool_calls:
            text += " " + " ".join(f"{c.name}({c.arguments})" for c in message.tool_calls)
        lines.append(f"{message.role}: {text.strip()}")
    return "\n".join(lines)
