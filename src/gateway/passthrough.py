"""A worker that hands each request straight to a backend that does its own scheduling.

Used with real model servers. See docs/12-real-models.md.
"""

from __future__ import annotations

from gateway.backends import Backend
from gateway.clock import Clock
from gateway.cost_model import estimate_cost
from gateway.overload import QueueFullError
from gateway.profiles import ModelProfile
from gateway.schema import GenerateRequest, RequestResult, TokenCallback


class PassthroughWorker:
    """Forwards requests to the backend concurrently and times what comes back.

    There is no queue, batch or prefix cache here: the model server behind the backend
    decides how requests are batched and when tokens are produced. This worker only stamps
    the clock when the request arrives, as each piece of output arrives, and at the end.
    """

    def __init__(
        self,
        profile: ModelProfile,
        backend: Backend,
        clock: Clock,
        *,
        max_in_flight: int | None = None,
        retry_after_ms: float = 1000.0,
    ) -> None:
        if max_in_flight is not None and max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        self._profile = profile
        self._backend = backend
        self._clock = clock
        self._max_in_flight = max_in_flight
        self._retry_after_ms = retry_after_ms
        self._in_flight = 0
        self._closed = False

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def waiting(self) -> int:
        """Always 0: nothing waits in the gateway. The model server has its own queue."""
        return 0

    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        if self._closed:
            raise RuntimeError("worker is closed")
        if self._max_in_flight is not None and self._in_flight >= self._max_in_flight:
            raise QueueFullError(
                self._profile.name, self._in_flight, self._max_in_flight, self._retry_after_ms
            )
        enqueue_ms = self._clock.now_ms()
        token_times: list[float] = []

        def stamp(index: int, text: str) -> None:
            now_ms = self._clock.now_ms()
            token_times.append(now_ms)
            if on_token is not None:
                on_token(index, now_ms, text)

        self._in_flight += 1
        try:
            generation = await self._backend.generate(request, on_token=stamp)
        finally:
            self._in_flight -= 1
        finish_ms = self._clock.now_ms()

        return RequestResult(
            request_id=request.request_id,
            model=self._profile.name,
            enqueue_ms=enqueue_ms,
            # The gateway does not queue; any wait inside the model server is part of the
            # time to first token and cannot be told apart from prompt processing.
            start_ms=enqueue_ms,
            first_token_ms=token_times[0] if token_times else finish_ms,
            finish_ms=finish_ms,
            tokens_in=generation.tokens_in,
            tokens_out=generation.tokens_out,
            cost_usd=estimate_cost(
                self._profile,
                new_prefill_tokens=generation.tokens_in,
                output_tokens=generation.tokens_out,
                batch_size=1,
            ).cost_usd,
            token_times_ms=tuple(token_times),
            text=generation.text,
        )

    async def aclose(self) -> None:
        self._closed = True
        await self._backend.aclose()
