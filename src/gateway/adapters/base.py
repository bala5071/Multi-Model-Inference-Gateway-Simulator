"""The base of the provider adapters: configuration, capabilities, and ``generate`` as one
chat turn, so an adapter can stand wherever a ``Backend`` can."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from gateway.adapters.http import DEFAULT_CONNECT_TIMEOUT_S, make_client
from gateway.backends.base import Backend, BackendTokenCallback, Generation
from gateway.chat import (
    UNKNOWN_CAPABILITIES,
    Capabilities,
    ChatDelta,
    ChatMessage,
    ChatRequest,
    ChatResult,
    Usage,
    estimate_input_tokens,
    estimate_tokens,
)
from gateway.schema import GenerateRequest

# 24 random hex characters: the ids the gateway gives tool calls that come without one.
TOOL_CALL_ID_HEX = 12


class HttpAdapter(Backend):
    """A provider behind HTTP. ``name`` identifies it in errors; ``model`` is what is sent as
    the provider's model; ``headers`` are fixed, non-secret headers from the configuration.
    The credential is never held here: each ``chat`` call is given it."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        headers: Mapping[str, str] | None = None,
        capabilities: Capabilities = UNKNOWN_CAPABILITIES,
        random_hex: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if not model:
            raise ValueError("an adapter needs a model")
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._headers = dict(headers or {})
        self._capabilities = capabilities
        self._random_hex = random_hex
        self._owns_client = client is None
        self._client = client or make_client(connect_timeout_s)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, model={self.model!r})"

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def new_tool_call_id(self) -> str:
        return "call_" + self._random_hex(TOOL_CALL_ID_HEX)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def usage(
        request: ChatRequest,
        reported_in: Any,
        reported_out: Any,
        content: str,
        arguments: str,
    ) -> Usage:
        """The provider's counts when it gave both, else an estimate marked as one."""
        if (
            isinstance(reported_in, int)
            and isinstance(reported_out, int)
            and reported_in >= 0
            and reported_out > 0
        ):
            return Usage(reported_in, reported_out)
        output = max(1, estimate_tokens(content + arguments))
        return Usage(estimate_input_tokens(request), output, estimated=True)

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        """One user message, answered as text."""
        pieces: list[str] = []

        def on_delta(delta: ChatDelta) -> None:
            if delta.text:
                if on_token is not None:
                    on_token(len(pieces), delta.text)
                pieces.append(delta.text)

        result: ChatResult = await self.chat(
            ChatRequest(
                request.request_id,
                request.model,
                (ChatMessage("user", request.prompt),),
                max_tokens=request.max_output_tokens,
            ),
            on_delta=on_delta,
        )
        return Generation(
            prompt_tokens=(),
            tokens_out=max(1, result.usage.output_tokens),
            text=result.content,
            prompt_token_count=result.usage.input_tokens,
        )
