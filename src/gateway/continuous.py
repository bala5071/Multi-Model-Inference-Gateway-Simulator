"""Iteration-level continuous batching for one model, with optional prefix reuse.

See docs/03-continuous-batching.md and docs/04-prefix-cache.md.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from gateway.backends import Backend, Generation, SimulatedBackend
from gateway.clock import Clock
from gateway.cost_model import decode_step_ms, estimate_cost
from gateway.overload import QueueFullError
from gateway.prefix_cache import PrefixCache, PrefixLease
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest, RequestResult, TokenCallback


class RequestTooLargeError(ValueError):
    """Raised for a request that cannot fit in the model's KV capacity even on its own."""

    def __init__(self, request_id: str, needed_tokens: int, capacity_tokens: int) -> None:
        self.request_id = request_id
        self.needed_tokens = needed_tokens
        self.capacity_tokens = capacity_tokens
        super().__init__(
            f"Request {request_id!r} needs {needed_tokens} KV tokens "
            f"but the model holds {capacity_tokens}"
        )


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One decode iteration, as seen just before it runs.

    ``batch_size`` counts the requests that decode in this step; requests still being
    prefilled are not in it. ``kv_tokens`` is everything held against the model's KV
    capacity: tokens reserved by running requests plus, if there is a prefix cache, the
    tokens it stores. ``prefill_tokens`` is how many prompt tokens were prefilled in chunks
    in this iteration, just before the step; it is 0 when chunked prefill is off.
    """

    start_ms: float
    step_ms: float
    batch_size: int
    kv_tokens: int
    prefill_tokens: int = 0


@dataclass(slots=True)
class _Queued:
    request: GenerateRequest
    enqueue_ms: float
    done: asyncio.Future[RequestResult]
    on_token: TokenCallback | None = None
    generation: Generation | None = None


@dataclass(slots=True)
class _Running:
    queued: _Queued
    generation: Generation
    start_ms: float
    cost_usd: float
    # KV tokens only this request holds: its uncached prompt tokens plus its whole output,
    # reserved up front so it can never outgrow the capacity mid-decode.
    private_tokens: int
    # Pins the cached prefix this request reuses; None when there is no prefix cache.
    lease: PrefixLease | None
    # Prompt tokens still to be prefilled. Always 0 unless prefill is chunked.
    prefill_remaining: int = 0
    token_times: list[float] = field(default_factory=list)


class ContinuousBatchingWorker:
    """Decodes many requests together, admitting and retiring them between decode steps.

    With a ``prefix_cache``, a request is prefilled and billed only for the prompt tokens
    the cache does not already hold, and the cache shares the model's KV capacity with the
    running requests.

    With ``prefill_chunk_tokens``, a new request's prompt is prefilled a chunk at a time,
    between decode steps, so a long prompt does not stall the requests already decoding.
    Without it, the whole prompt is prefilled at admission while the batch waits.
    """

    def __init__(
        self,
        profile: ModelProfile,
        backend: Backend,
        clock: Clock,
        *,
        prefix_cache: PrefixCache | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        max_queue_len: int | None = None,
        retry_after_ms: float = 1000.0,
        prefill_chunk_tokens: int | None = None,
    ) -> None:
        if max_queue_len is not None and max_queue_len < 0:
            raise ValueError("max_queue_len must not be negative")
        if prefill_chunk_tokens is not None and prefill_chunk_tokens < 1:
            raise ValueError("prefill_chunk_tokens must be at least 1")
        if prefix_cache is not None and prefix_cache.capacity_tokens != profile.kv_capacity_tokens:
            raise ValueError("prefix_cache capacity must equal the profile's kv_capacity_tokens")
        self._profile = profile
        self._backend = backend
        self._clock = clock
        self._cache = prefix_cache
        self._on_step = on_step
        self._max_queue_len = max_queue_len
        self._retry_after_ms = retry_after_ms
        self._chunk_tokens = prefill_chunk_tokens
        self._queue: deque[_Queued] = deque()
        self._running: list[_Running] = []
        self._reserved_tokens = 0
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    @property
    def prefix_cache(self) -> PrefixCache | None:
        return self._cache

    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        if self._closed:
            raise RuntimeError("worker is closed")
        if self._max_queue_len is not None and self._waiting(arriving=1) > self._max_queue_len:
            raise QueueFullError(
                self._profile.name, self.waiting, self._max_queue_len, self._retry_after_ms
            )
        done: asyncio.Future[RequestResult] = asyncio.get_running_loop().create_future()
        self._queue.append(_Queued(request, self._clock.now_ms(), done, on_token))
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run())
        return await done

    @property
    def waiting(self) -> int:
        """Requests that are queued and cannot be seated in a free batch slot right now.

        A request that has just arrived sits in the queue until the scheduler's next
        iteration even when the batch has room, so free slots are subtracted. Requests
        whose caller has given up are not counted.
        """
        return self._waiting(arriving=0)

    def _waiting(self, *, arriving: int) -> int:
        queued = sum(1 for q in self._queue if not q.done.cancelled()) + arriving
        running = sum(1 for r in self._running if not r.queued.done.cancelled())
        return max(0, queued - (self._profile.max_batch_size - running))

    async def aclose(self) -> None:
        self._closed = True
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for running in self._running:
            self._free(running)
            running.queued.done.cancel()
        self._running = []
        while self._queue:
            self._queue.popleft().done.cancel()

    async def _run(self) -> None:
        while True:
            self._drop_cancelled()
            await self._admit()
            # Again: a request can be cancelled while another one is being prefilled.
            self._drop_cancelled()
            if not self._running:
                if self._queue:
                    continue
                # Nothing was admissible, so the queue is empty: anything that could not
                # fit into an empty batch has been rejected.
                return
            prefill_tokens = await self._prefill_chunks()
            if prefill_tokens:
                self._drop_cancelled()
            await self._decode_step(prefill_tokens)

    def _kv_tokens(self) -> int:
        cached = self._cache.size_tokens if self._cache is not None else 0
        return self._reserved_tokens + cached

    def _free(self, running: _Running) -> None:
        self._reserved_tokens -= running.private_tokens
        if self._cache is not None and running.lease is not None:
            self._cache.release(running.lease)

    def _drop_cancelled(self) -> None:
        """Remove running requests whose caller has given up, freeing their slot and KV tokens.

        Cancelled requests still in the queue are skipped when they reach its head, and
        are not counted by ``waiting``.
        """
        kept: list[_Running] = []
        for running in self._running:
            if running.queued.done.cancelled():
                self._free(running)
            else:
                kept.append(running)
        self._running = kept

    async def _admit(self) -> None:
        """Admit from the head of the queue while the batch and the KV capacity have room.

        Strictly first come, first served: if the head does not fit, nothing behind it is
        admitted either. Without chunked prefill, each admitted request is prefilled
        here, one after another, and the running batch waits while that happens. With it,
        admission takes no time and the prompt is left for ``_prefill_chunks``.
        """
        capacity = self._profile.kv_capacity_tokens
        while self._queue and len(self._running) < self._profile.max_batch_size:
            head = self._queue[0]
            if head.done.cancelled():
                self._queue.popleft()
                continue
            if head.generation is None:
                try:
                    head.generation = await self._backend.generate(head.request)
                except Exception as exc:
                    self._queue.popleft()
                    if not head.done.done():
                        head.done.set_exception(exc)
                    continue
            generation = head.generation
            footprint = generation.tokens_in + generation.tokens_out
            if footprint > capacity:
                self._queue.popleft()
                if not head.done.done():
                    head.done.set_exception(
                        RequestTooLargeError(head.request.request_id, footprint, capacity)
                    )
                continue

            has_room, lease = self._reserve(generation)
            if not has_room:
                return
            cached_tokens = lease.matched_tokens if lease is not None else 0

            self._queue.popleft()
            estimate = estimate_cost(
                self._profile,
                new_prefill_tokens=generation.tokens_in - cached_tokens,
                output_tokens=generation.tokens_out,
                batch_size=1,
            )
            self._running.append(
                _Running(
                    queued=head,
                    generation=generation,
                    start_ms=self._clock.now_ms(),
                    cost_usd=estimate.cost_usd,
                    private_tokens=footprint - cached_tokens,
                    lease=lease,
                    prefill_remaining=(
                        generation.tokens_in - cached_tokens
                        if self._chunk_tokens is not None
                        else 0
                    ),
                )
            )
            if self._chunk_tokens is None:
                await self._clock.sleep_ms(estimate.prefill_ms)

    async def _prefill_chunks(self) -> int:
        """Prefill one chunk for every request that still has prompt tokens to go.

        Each such request advances by up to ``prefill_chunk_tokens`` per iteration. The
        chunks are processed one after another, so the iteration is delayed by their
        total. Returns the number of tokens prefilled; 0 when chunked prefill is off.
        """
        if self._chunk_tokens is None:
            return 0
        tokens = 0
        for running in self._running:
            chunk = min(self._chunk_tokens, running.prefill_remaining)
            running.prefill_remaining -= chunk
            tokens += chunk
        if tokens:
            await self._clock.sleep_ms(tokens * self._profile.prefill_ms_per_token)
        return tokens

    def _reserve(self, generation: Generation) -> tuple[bool, PrefixLease | None]:
        """Reserve KV room for a request, evicting unused cached prefixes if needed.

        Returns ``(has_room, lease)``. ``has_room`` is False if the request has to wait
        for running requests to finish. ``lease`` pins the reused prefix; it is None
        without a cache or without room.
        """
        capacity = self._profile.kv_capacity_tokens
        footprint = generation.tokens_in + generation.tokens_out
        if self._cache is None:
            if self._reserved_tokens + footprint > capacity:
                return False, None
            self._reserved_tokens += footprint
            return True, None

        # The lease pins the matched prefix first, so the eviction below cannot take it.
        lease = self._cache.acquire(generation.prompt_tokens)
        private_tokens = footprint - lease.matched_tokens
        shortfall = self._kv_tokens() + private_tokens - capacity
        if shortfall > self._cache.evictable_tokens:
            self._cache.abandon(lease)
            return False, None
        if shortfall > 0:
            self._cache.evict(shortfall)
        self._reserved_tokens += private_tokens
        return True, lease

    async def _decode_step(self, prefill_tokens: int = 0) -> None:
        """One decode step: every request that has finished prefilling gains one token.

        Finished requests leave. Requests still being prefilled are untouched; if every
        running request is still prefilling, there is no step at all.
        """
        decoding = sum(1 for r in self._running if r.prefill_remaining == 0)
        if not decoding:
            return
        step_ms = decode_step_ms(self._profile, decoding)
        if self._on_step is not None:
            self._on_step(
                StepRecord(
                    self._clock.now_ms(), step_ms, decoding, self._kv_tokens(), prefill_tokens
                )
            )
        await self._clock.sleep_ms(step_ms)
        now_ms = self._clock.now_ms()

        still_running: list[_Running] = []
        for running in self._running:
            if running.prefill_remaining:
                still_running.append(running)
                continue
            running.token_times.append(now_ms)
            if running.queued.on_token is not None and not running.queued.done.cancelled():
                index = len(running.token_times) - 1
                running.queued.on_token(index, now_ms, f"tok{index}")
            if len(running.token_times) < running.generation.tokens_out:
                still_running.append(running)
                continue
            self._finish(running, now_ms)
        self._running = still_running

    def _finish(self, running: _Running, now_ms: float) -> None:
        cached_tokens = running.lease.matched_tokens if running.lease is not None else 0
        if self._cache is not None:
            # The prompt's KV entries move from this request's reservation into the cache.
            # They always fit without eviction: the reservation given up just below is at
            # least as large as what the cache gains.
            self._cache.insert(running.generation.prompt_tokens)
        self._free(running)
        if not running.queued.done.done():
            running.queued.done.set_result(
                RequestResult(
                    request_id=running.queued.request.request_id,
                    model=self._profile.name,
                    enqueue_ms=running.queued.enqueue_ms,
                    start_ms=running.start_ms,
                    first_token_ms=running.token_times[0],
                    finish_ms=now_ms,
                    tokens_in=running.generation.tokens_in,
                    tokens_out=running.generation.tokens_out,
                    cost_usd=running.cost_usd,
                    cached_tokens=cached_tokens,
                    token_times_ms=tuple(running.token_times),
                )
            )


def build_continuous_registry(
    clock: Clock,
    *,
    seed: int,
    prefix_cache: bool = True,
    profiles: Mapping[Tier, ModelProfile] = PROFILES,
    max_queue_len: Mapping[Tier, int] | None = None,
    prefill_chunk_tokens: int | None = None,
) -> ModelRegistry:
    """One continuous batching worker per tier; same names and aliases as the baseline.

    Each model gets its own prefix cache, sized to its KV capacity, unless
    ``prefix_cache`` is False. ``profiles`` replaces the standard model profiles.
    ``max_queue_len`` limits each listed model's queue; other models are unlimited.
    ``prefill_chunk_tokens`` turns on chunked prefill for every model.
    """
    backend = SimulatedBackend(seed=seed)
    registry = ModelRegistry()
    for tier, profile in profiles.items():
        cache = PrefixCache(profile.kv_capacity_tokens) if prefix_cache else None
        worker = ContinuousBatchingWorker(
            profile,
            backend,
            clock,
            prefix_cache=cache,
            max_queue_len=max_queue_len.get(tier) if max_queue_len is not None else None,
            prefill_chunk_tokens=prefill_chunk_tokens,
        )
        registry.register(tier.value, worker, aliases=[profile.name])
    return registry
