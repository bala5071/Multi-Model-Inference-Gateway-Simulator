"""The baseline worker: no batching, no prefix reuse. Later steps must beat it.

See docs/02-baseline.md.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from gateway.backends import Backend, SimulatedBackend
from gateway.clock import Clock
from gateway.cost_model import estimate_cost
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest, RequestResult, TokenCallback


@dataclass(frozen=True, slots=True)
class _Queued:
    request: GenerateRequest
    enqueue_ms: float
    done: asyncio.Future[RequestResult]
    on_token: TokenCallback | None = None


class BaselineWorker:
    """Serves one request at a time, start to finish, in arrival order."""

    def __init__(self, profile: ModelProfile, backend: Backend, clock: Clock) -> None:
        self._profile = profile
        self._backend = backend
        self._clock = clock
        self._queue: deque[_Queued] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        if self._closed:
            raise RuntimeError("worker is closed")
        enqueue_ms = self._clock.now_ms()
        # The fixed per-request time passes before the request joins the queue, so it
        # is part of its queue wait and does not hold up the request being served.
        if self._profile.per_request_overhead_ms > 0:
            await self._clock.sleep_ms(self._profile.per_request_overhead_ms)
            if self._closed:
                raise RuntimeError("worker is closed")
        done: asyncio.Future[RequestResult] = asyncio.get_running_loop().create_future()
        self._queue.append(_Queued(request, enqueue_ms, done, on_token))
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())
        return await done

    async def aclose(self) -> None:
        self._closed = True
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
        while self._queue:
            self._queue.popleft().done.cancel()

    async def _drain(self) -> None:
        while self._queue:
            queued = self._queue.popleft()
            if queued.done.cancelled():
                continue
            try:
                result = await self._serve(queued)
            except asyncio.CancelledError:
                queued.done.cancel()
                raise
            except Exception as exc:
                # One bad request must not stop the queue behind it.
                if not queued.done.done():
                    queued.done.set_exception(exc)
            else:
                if not queued.done.done():
                    queued.done.set_result(result)

    async def _serve(self, queued: _Queued) -> RequestResult:
        start_ms = self._clock.now_ms()
        generation = await self._backend.generate(queued.request)
        estimate = estimate_cost(
            self._profile,
            new_prefill_tokens=generation.tokens_in,
            output_tokens=generation.tokens_out,
            batch_size=1,
        )

        await self._clock.sleep_ms(estimate.prefill_ms)
        token_times: list[float] = []
        for index in range(generation.tokens_out):
            await self._clock.sleep_ms(estimate.decode_step_ms)
            token_times.append(self._clock.now_ms())
            if queued.on_token is not None:
                queued.on_token(index, token_times[-1], f"tok{index}")

        return RequestResult(
            request_id=queued.request.request_id,
            model=self._profile.name,
            enqueue_ms=queued.enqueue_ms,
            start_ms=start_ms,
            first_token_ms=token_times[0],
            finish_ms=token_times[-1],
            tokens_in=generation.tokens_in,
            tokens_out=generation.tokens_out,
            cost_usd=estimate.cost_usd,
            token_times_ms=tuple(token_times),
        )


def build_baseline_registry(
    clock: Clock, *, seed: int, profiles: Mapping[Tier, ModelProfile] = PROFILES
) -> ModelRegistry:
    """One baseline worker per tier, named by tier, with the model name as an alias.

    ``profiles`` replaces the standard model profiles.
    """
    backend = SimulatedBackend(seed=seed)
    registry = ModelRegistry()
    for tier, profile in profiles.items():
        registry.register(
            tier.value, BaselineWorker(profile, backend, clock), aliases=[profile.name]
        )
    return registry
