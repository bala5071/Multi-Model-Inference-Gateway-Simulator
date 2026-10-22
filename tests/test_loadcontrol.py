"""The in-server load controller, on simulated time with a recording sender."""

from __future__ import annotations

import dataclasses

import pytest

from gateway.clock import SimClock
from gateway.loadcontrol import (
    PRESETS,
    LoadController,
    LoadStatus,
    Phase,
    Preset,
    UnknownPresetError,
)
from gateway.workload import LoadConfig, PlannedRequest, build_workload

QUIET = LoadConfig(total_requests=5, arrival_rate_per_s=10.0, max_tokens=4)
BUSY = LoadConfig(total_requests=8, arrival_rate_per_s=100.0, max_tokens=4)
TEST_PRESETS = {
    "one": Preset("one", "a single phase", (Phase(QUIET),)),
    "two": Preset("two", "quiet, then busy", (Phase(QUIET), Phase(BUSY, allow_downgrade=True))),
}


@dataclasses.dataclass
class Sent:
    at_ms: float
    request: PlannedRequest
    allow_downgrade: bool


class Recorder:
    """A sender that takes ``service_ms`` per request and refuses every third one."""

    def __init__(self, clock: SimClock, service_ms: float = 20.0) -> None:
        self.clock = clock
        self.service_ms = service_ms
        self.sent: list[Sent] = []
        self.finished = 0

    async def __call__(self, request: PlannedRequest, allow_downgrade: bool) -> bool:
        self.sent.append(Sent(self.clock.now_ms(), request, allow_downgrade))
        await self.clock.sleep_ms(self.service_ms)
        self.finished += 1
        return request.index % 3 != 2


def _controller(clock: SimClock, **kwargs: int) -> tuple[LoadController, Recorder]:
    recorder = Recorder(clock)
    return LoadController(clock, recorder, presets=TEST_PRESETS, **kwargs), recorder


async def test_requests_are_sent_at_the_times_the_seeded_workload_plans() -> None:
    clock = SimClock()
    controller, recorder = _controller(clock)
    await clock.sleep_ms(500.0)

    await controller.start("one", 7)
    planned = build_workload(dataclasses.replace(QUIET, seed=7000))
    await clock.sleep_ms(planned[-1].at_ms + 1.0)
    await controller.stop()

    first_phase = recorder.sent[:5]
    assert [s.request for s in first_phase] == planned
    # Each at its planned offset from when the phase began, 500 ms.
    assert [s.at_ms for s in first_phase] == pytest.approx(
        [500.0 + r.at_ms for r in planned], abs=1e-6
    )
    assert all(s.allow_downgrade is False for s in first_phase)


async def test_phases_follow_each_other_and_the_cycle_repeats_with_new_seeds() -> None:
    clock = SimClock()
    controller, recorder = _controller(clock)

    await controller.start("two", 3)
    await clock.sleep_ms(3000.0)
    status = await controller.stop()

    def plan(config: LoadConfig, number: int) -> list[PlannedRequest]:
        return build_workload(dataclasses.replace(config, seed=3000 + number))

    expected = [*plan(QUIET, 0), *plan(BUSY, 1), *plan(QUIET, 2), *plan(BUSY, 3)]
    assert len(recorder.sent) > len(expected)
    assert [s.request for s in recorder.sent[: len(expected)]] == expected
    # The busy phase's requests carry its flag; the quiet phase's do not.
    assert [s.allow_downgrade for s in recorder.sent[:13]] == [False] * 5 + [True] * 8
    # No two phases repeat each other's prompts.
    prompts = [s.request.prompt for s in recorder.sent]
    assert len(set(prompts)) == len(prompts)
    assert status.phases_started == len(recorder.sent) // 13 * 2 + (
        1 if len(recorder.sent) % 13 <= 5 else 2
    )


async def test_the_same_preset_and_seed_send_the_same_requests_at_the_same_times() -> None:
    async def run(seed: int) -> list[tuple[float, str, str]]:
        clock = SimClock()
        controller, recorder = _controller(clock)
        await controller.start("two", seed)
        await clock.sleep_ms(2000.0)
        await controller.stop()
        return [(s.at_ms, s.request.model, s.request.prompt) for s in recorder.sent]

    first, again, other = await run(11), await run(11), await run(12)

    assert first == again
    assert first != other


async def test_status_counts_what_was_sent_served_and_refused() -> None:
    clock = SimClock()
    controller, recorder = _controller(clock)
    assert controller.status() == LoadStatus(False, None, None, None, 0, 0, 0, 0, 0, 0)

    await clock.sleep_ms(250.0)
    started = await controller.start("one", 7)
    assert started == LoadStatus(True, "one", 7, 250.0, 0, 0, 0, 0, 0, 0)

    planned = build_workload(dataclasses.replace(QUIET, seed=7000))
    # Past the fifth request's arrival and its 20 ms of service, before the next phase's
    # first request can have finished.
    await clock.sleep_ms(planned[-1].at_ms + 20.5)
    status = controller.status()

    assert (status.running, status.preset, status.seed, status.started_ms) == (
        True,
        "one",
        7,
        250.0,
    )
    assert status.sent == len(recorder.sent)
    assert status.served + status.refused == recorder.finished
    assert status.in_progress == status.sent - recorder.finished
    # Of the first five (indices 0 to 4), the one with index 2 was refused.
    assert recorder.finished >= 5
    assert status.refused >= 1
    assert status.skipped == 0
    await controller.stop()


async def test_stop_cancels_requests_in_progress_and_sends_nothing_more() -> None:
    clock = SimClock()
    recorder = Recorder(clock, service_ms=10_000.0)
    controller = LoadController(clock, recorder, presets=TEST_PRESETS)

    await controller.start("two", 5)
    await clock.sleep_ms(300.0)
    assert controller.status().in_progress > 0

    stopped = await controller.stop()
    sent = len(recorder.sent)
    await clock.sleep_ms(60_000.0)

    assert (stopped.running, stopped.in_progress) == (False, 0)
    # What it did is still reported after it stopped.
    assert (stopped.preset, stopped.seed, stopped.sent) == ("two", 5, sent)
    assert len(recorder.sent) == sent
    assert recorder.finished == 0
    # Stopping again is harmless.
    assert (await controller.stop()).running is False


async def test_starting_again_replaces_the_run() -> None:
    clock = SimClock()
    controller, recorder = _controller(clock)

    await controller.start("one", 1)
    await clock.sleep_ms(200.0)
    restarted = await controller.start("two", 2)

    assert (restarted.preset, restarted.seed, restarted.sent, restarted.started_ms) == (
        "two",
        2,
        0,
        200.0,
    )
    before = len(recorder.sent)
    await clock.sleep_ms(300.0)
    await controller.stop()
    # Everything sent since belongs to the new run's seed.
    assert all("s2000r" in s.request.prompt for s in recorder.sent[before:])
    assert len(recorder.sent) > before


async def test_requests_over_the_in_progress_bound_are_skipped_and_counted() -> None:
    clock = SimClock()
    recorder = Recorder(clock, service_ms=10_000.0)
    controller = LoadController(clock, recorder, presets=TEST_PRESETS, max_in_progress=3)

    await controller.start("one", 7)
    await clock.sleep_ms(2000.0)
    status = await controller.stop()

    assert len(recorder.sent) == 3
    assert status.sent == 3
    assert status.skipped > 0


async def test_unknown_preset_is_an_error_and_leaves_the_current_run_alone() -> None:
    clock = SimClock()
    controller, _ = _controller(clock)
    await controller.start("one", 7)

    with pytest.raises(UnknownPresetError, match="Unknown load preset 'tsunami'") as raised:
        await controller.start("tsunami", 1)

    assert raised.value.valid == ("one", "two")
    assert controller.status().preset == "one"
    assert controller.status().running is True
    await controller.stop()


def test_the_packaged_presets_are_the_four_the_dashboard_offers() -> None:
    assert list(PRESETS) == ["steady", "burst", "overload", "long-prompts"]
    assert all(preset.name == name and preset.description for name, preset in PRESETS.items())
    assert LoadController(SimClock(), Recorder(SimClock())).presets is PRESETS
    with pytest.raises(ValueError, match="max_in_progress"):
        LoadController(SimClock(), Recorder(SimClock()), max_in_progress=0)
