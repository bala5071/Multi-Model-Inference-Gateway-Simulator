"""Serving one chat request on one tier: through the gateway's own scheduler (the simulator) or
through the tier's provider adapter, with first-token and total timeouts on the injected clock.

See docs/31-chat-and-adapters.md.

- **The simulator** takes only a prompt: the conversation is flattened into one, served by the
  same worker and scheduler as ``/v1/generate``, and answered ``[simulated output, N tokens]``.
  It has no tools (``supports_tools`` false, built in).
- **An adapter** is given the request and no credential in this step (the operator's own
  servers take none; organization keys arrive with the vault, docs/provider-plan.md). Waiting
  for the first delta is bounded by ``first_token_timeout_ms``, the whole answer by
  ``total_timeout_ms``; when either passes, or the caller is cancelled (the client went away),
  the adapter's task is cancelled, which closes the upstream request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from gateway.backends import Backend
from gateway.chat import (
    Capabilities,
    ChatDelta,
    ChatDeltaCallback,
    ChatRequest,
    ChatResult,
    FinishReason,
    ProviderError,
    ProviderErrorCode,
    Source,
    Usage,
    flatten_messages,
)
from gateway.clock import Clock
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest, RequestResult

DEFAULT_FIRST_TOKEN_TIMEOUT_MS = 30_000.0
DEFAULT_TOTAL_TIMEOUT_MS = 120_000.0
LOCAL_PROVIDER = "local"
SIMULATED_CAPABILITIES = Capabilities(
    supports_tools=False,
    supports_streaming=True,
    context_window=None,
    sources={
        "supports_tools": Source.BUILT_IN,
        "supports_streaming": Source.BUILT_IN,
        "context_window": Source.UNKNOWN,
    },
    reasons={"supports_tools": "simulated_model"},
)


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    result: ChatResult
    # When it arrived, started, first answered and finished, and its tokens, for metrics and
    # the ledger.
    timing: RequestResult
    provider: str


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        clock: Clock,
        adapters: Mapping[str, Backend] | None = None,
        *,
        first_token_timeout_ms: float = DEFAULT_FIRST_TOKEN_TIMEOUT_MS,
        total_timeout_ms: float = DEFAULT_TOTAL_TIMEOUT_MS,
    ) -> None:
        if not 0 < first_token_timeout_ms <= total_timeout_ms:
            raise ValueError("the first-token timeout must be positive and within the total")
        self._registry = registry
        self._clock = clock
        self._adapters = dict(adapters or {})
        self.first_token_timeout_ms = first_token_timeout_ms
        self.total_timeout_ms = total_timeout_ms

    def capabilities(self, tier: str) -> Capabilities:
        adapter = self._adapters.get(tier)
        return adapter.capabilities() if adapter is not None else SIMULATED_CAPABILITIES

    async def run(
        self, request: ChatRequest, on_delta: ChatDeltaCallback | None = None
    ) -> ChatOutcome:
        adapter = self._adapters.get(request.model)
        if adapter is None:
            return await self._simulated(request, on_delta)
        return await self._adapter(adapter, request, on_delta)

    async def _simulated(
        self, request: ChatRequest, on_delta: ChatDeltaCallback | None
    ) -> ChatOutcome:
        timing = await self._registry.submit(
            GenerateRequest(
                request.request_id,
                request.model,
                flatten_messages(request.messages),
                request.max_tokens,
            )
        )
        text = f"[simulated output, {timing.tokens_out} tokens]"
        if on_delta is not None:
            on_delta(ChatDelta(text=text))
        finish: FinishReason = (
            "length"
            if request.max_tokens is not None and timing.tokens_out >= request.max_tokens
            else "stop"
        )
        result = ChatResult(text, (), finish, Usage(timing.tokens_in, timing.tokens_out))
        return ChatOutcome(result, timing, LOCAL_PROVIDER)

    async def _adapter(
        self, adapter: Backend, request: ChatRequest, on_delta: ChatDeltaCallback | None
    ) -> ChatOutcome:
        clock = self._clock
        started = clock.now_ms()
        first_ms: list[float] = []
        first = asyncio.Event()

        def relay(delta: ChatDelta) -> None:
            if not first_ms:
                first_ms.append(clock.now_ms())
                first.set()
            if on_delta is not None:
                on_delta(delta)

        task = asyncio.ensure_future(adapter.chat(request, credential=None, on_delta=relay))
        try:
            if not await self._within(task, first, self.first_token_timeout_ms):
                raise ProviderError(ProviderErrorCode.UPSTREAM_TIMEOUT, detail="first_token")
            remaining = self.total_timeout_ms - (clock.now_ms() - started)
            if not await self._within(task, None, remaining):
                raise ProviderError(ProviderErrorCode.UPSTREAM_TIMEOUT, detail="total")
            result = task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finished = clock.now_ms()
        timing = RequestResult(
            request_id=request.request_id,
            model=request.model,
            enqueue_ms=started,
            start_ms=started,
            first_token_ms=first_ms[0] if first_ms else finished,
            finish_ms=finished,
            tokens_in=result.usage.input_tokens,
            tokens_out=result.usage.output_tokens,
            cost_usd=0.0,
            text=result.content,
        )
        return ChatOutcome(result, timing, LOCAL_PROVIDER)

    async def _within(
        self, task: asyncio.Future[ChatResult], event: asyncio.Event | None, timeout_ms: float
    ) -> bool:
        """Wait until ``task`` is done or ``event`` is set, at most ``timeout_ms`` on the
        clock. False if the time ran out first."""
        if task.done() or (event is not None and event.is_set()):
            return True
        if timeout_ms <= 0:
            return False
        sleeper = asyncio.ensure_future(self._clock.sleep_ms(timeout_ms))
        waiters: set[asyncio.Future[object]] = {task, sleeper}  # type: ignore[arg-type]
        setter = asyncio.ensure_future(event.wait()) if event is not None else None
        if setter is not None:
            waiters.add(setter)  # type: ignore[arg-type]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (sleeper, setter):
                if waiter is not None and not waiter.done():
                    waiter.cancel()
        return task.done() or (event is not None and event.is_set())
