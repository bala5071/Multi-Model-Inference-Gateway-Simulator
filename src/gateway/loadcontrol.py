"""Runs the seeded load generator inside the server, for the dashboard's load control.

See docs/17-playground-load-replay.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from gateway.clock import Clock
from gateway.workload import LengthBucket, LoadConfig, PlannedRequest, build_workload


@dataclass(frozen=True, slots=True)
class Phase:
    """One stretch of a preset: a workload config, and whether its requests may be moved."""

    config: LoadConfig
    allow_downgrade: bool = False


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    description: str
    # Run in order, then again from the first, until the load is stopped.
    phases: tuple[Phase, ...]


# Sized against the calibrated profiles with batches of at most 4, where premium serves
# about 0.23 requests a second on the default mix. "steady" and "long-prompts" keep every
# model under 60% of its capacity (benchmarks/saturation.py); "burst" briefly goes over and
# recovers; "overload" is overloaded on purpose. They are demo settings, not measurements.
# The seeds in these configs are placeholders; ``LoadController`` replaces them.
PRESETS: Mapping[str, Preset] = {
    preset.name: preset
    for preset in (
        Preset(
            "steady",
            "0.8 requests a second across the three models, the load generator's default mix.",
            (Phase(LoadConfig(total_requests=240, arrival_rate_per_s=0.8)),),
        ),
        Preset(
            "burst",
            "75 quiet seconds at 0.4 requests a second, then 10 seconds at 3.",
            (
                Phase(LoadConfig(total_requests=30, arrival_rate_per_s=0.4)),
                Phase(LoadConfig(total_requests=30, arrival_rate_per_s=3.0)),
            ),
        ),
        Preset(
            "overload",
            "Mostly premium, at about three times what it can serve; requests may be downgraded.",
            (
                Phase(
                    LoadConfig(
                        total_requests=180,
                        arrival_rate_per_s=1.0,
                        model_weights={"economy": 10, "standard": 10, "premium": 80},
                    ),
                    allow_downgrade=True,
                ),
            ),
        ),
        Preset(
            "long-prompts",
            "Mostly prompts of 300 to 800 words, most of them sharing a system prompt.",
            (
                Phase(
                    LoadConfig(
                        total_requests=120,
                        arrival_rate_per_s=0.4,
                        prompt_lengths=(LengthBucket(300, 800, 0.8), LengthBucket(20, 80, 0.2)),
                        shared_prefix_fraction=0.8,
                    )
                ),
            ),
        ),
    )
}

# Sends one planned request and says whether it was served. ``allow_downgrade`` is the
# phase's. It must not raise for a request the gateway refused.
Sender = Callable[[PlannedRequest, bool], Awaitable[bool]]


class UnknownPresetError(LookupError):
    def __init__(self, name: str, valid: Sequence[str]) -> None:
        self.name = name
        self.valid = tuple(valid)
        super().__init__(f"Unknown load preset {name!r}. Presets: {', '.join(self.valid)}.")


@dataclass(frozen=True, slots=True)
class LoadStatus:
    running: bool
    preset: str | None
    seed: int | None
    # Clock time the run started at.
    started_ms: float | None
    # How many phases have been planned so far, counting repeats of the cycle.
    phases_started: int
    sent: int
    served: int
    refused: int
    # Not sent because too many of this run's requests were still in progress.
    skipped: int
    in_progress: int


class LoadController:
    """Starts and stops one load run at a time, paced on the injected clock.

    A run plays its preset's phases in a cycle until stopped. Phase ``n`` of a run (counting
    from 0, across cycles) is planned by ``build_workload`` with seed ``seed * 1000 + n``,
    so a run is a pure function of its preset and seed, and no two phases repeat each
    other's prompts. Requests are sent one after another at their planned times by a single
    task; each then runs concurrently.

    ``max_in_progress`` bounds how many of the run's requests may be unfinished at once. A
    gateway with queue limits refuses long before that; without them a request due while
    the bound is reached is skipped and counted.
    """

    def __init__(
        self,
        clock: Clock,
        send: Sender,
        *,
        presets: Mapping[str, Preset] = PRESETS,
        max_in_progress: int = 2000,
    ) -> None:
        if max_in_progress < 1:
            raise ValueError("max_in_progress must be at least 1")
        self._clock = clock
        self._send = send
        self._presets = presets
        self._max_in_progress = max_in_progress
        self._runner: asyncio.Task[None] | None = None
        self._requests: set[asyncio.Task[None]] = set()
        self._status = LoadStatus(False, None, None, None, 0, 0, 0, 0, 0, 0)

    @property
    def presets(self) -> Mapping[str, Preset]:
        return self._presets

    def status(self) -> LoadStatus:
        return dataclasses.replace(self._status, in_progress=len(self._requests))

    async def start(self, preset: str, seed: int) -> LoadStatus:
        """Start a run, stopping the current one first."""
        chosen = self._presets.get(preset)
        if chosen is None:
            raise UnknownPresetError(preset, tuple(self._presets))
        await self.stop()
        self._status = LoadStatus(True, preset, seed, self._clock.now_ms(), 0, 0, 0, 0, 0, 0)
        self._runner = asyncio.create_task(self._run(chosen, seed))
        return self.status()

    async def stop(self) -> LoadStatus:
        """Stop the run and cancel its requests that are still in progress."""
        tasks = [*self._requests]
        if self._runner is not None:
            tasks.append(self._runner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runner = None
        self._requests.clear()
        self._status = dataclasses.replace(self._status, running=False)
        return self.status()

    def _count(self, **changes: int) -> None:
        current = dataclasses.asdict(self._status)
        self._status = dataclasses.replace(
            self._status, **{name: current[name] + by for name, by in changes.items()}
        )

    async def _run(self, preset: Preset, seed: int) -> None:
        number = 0
        while True:
            phase = preset.phases[number % len(preset.phases)]
            config = dataclasses.replace(phase.config, seed=seed * 1000 + number)
            self._count(phases_started=1)
            number += 1
            phase_start_ms = self._clock.now_ms()
            for request in build_workload(config):
                wait_ms = phase_start_ms + request.at_ms - self._clock.now_ms()
                if wait_ms > 0:
                    await self._clock.sleep_ms(wait_ms)
                if len(self._requests) >= self._max_in_progress:
                    self._count(skipped=1)
                    continue
                self._count(sent=1)
                task = asyncio.create_task(self._one(request, phase.allow_downgrade))
                self._requests.add(task)
                task.add_done_callback(self._requests.discard)

    async def _one(self, request: PlannedRequest, allow_downgrade: bool) -> None:
        served = await self._send(request, allow_downgrade)
        self._count(**{"served" if served else "refused": 1})
