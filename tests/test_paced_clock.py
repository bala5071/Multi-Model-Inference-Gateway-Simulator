"""PacedSimClock: simulated time let through at a multiple of real time."""

from __future__ import annotations

import asyncio

import pytest

from gateway.clock import Clock, PacedSimClock, SimClock


class Waits:
    """Stands in for the real sleep: records what was asked for and returns at once."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


async def _script(clock: Clock) -> list[tuple[str, float]]:
    """A few tasks sleeping for different times, some of them repeatedly."""
    log: list[tuple[str, float]] = []

    async def sleeper(name: str, *durations_ms: float) -> None:
        for duration_ms in durations_ms:
            await clock.sleep_ms(duration_ms)
            log.append((name, clock.now_ms()))

    await asyncio.gather(
        sleeper("a", 100.0, 250.0, 0.5),
        sleeper("b", 100.0, 1000.0),
        sleeper("c", 33.25, 33.25, 33.25),
        sleeper("d", 5000.0),
    )
    return log


@pytest.mark.parametrize("speed", [1.0, 5.0, 20.0, 0.3])
async def test_sleepers_wake_at_the_same_simulated_times_as_on_a_sim_clock(speed: float) -> None:
    expected = await _script(SimClock())

    paced = await _script(PacedSimClock(speed, wait=Waits()))

    assert paced == expected
    assert paced[-1] == ("d", 5000.0)


async def test_real_waiting_is_the_simulated_gap_divided_by_the_speed() -> None:
    waits = Waits()
    clock = PacedSimClock(20.0, slice_s=0.02, min_wait_s=0.0, wait=waits)

    await asyncio.gather(clock.sleep_ms(100.0), clock.sleep_ms(1000.0))

    # 100 ms at 20x is 5 ms of real time. The 900 ms that follow are taken in slices of
    # 20 real ms (400 simulated ms each), and the last slice is what is left.
    assert waits.seconds == pytest.approx([0.005, 0.02, 0.02, 0.005])
    assert sum(waits.seconds) == pytest.approx(1000.0 / 20.0 / 1000.0)
    assert clock.now_ms() == 1000.0


@pytest.mark.parametrize("speed", [1.0, 5.0, 20.0])
async def test_total_real_time_scales_with_the_speed(speed: float) -> None:
    waits = Waits()
    await _script(PacedSimClock(speed, wait=waits))
    assert sum(waits.seconds) == pytest.approx(5.0 / speed)


async def test_speed_can_be_changed_while_running() -> None:
    waits = Waits()
    clock = PacedSimClock(1.0, slice_s=10.0, wait=waits)

    await clock.sleep_ms(1000.0)
    clock.speed = 20.0
    await clock.sleep_ms(1000.0)

    assert waits.seconds == pytest.approx([1.0, 0.05])
    assert clock.now_ms() == 2000.0


async def test_something_arriving_between_two_wake_ups_sees_time_moving_and_is_woken_on_time() -> (
    None
):
    seen: list[float] = []
    calls = 0
    clock: PacedSimClock

    async def visitor() -> None:
        # Like an HTTP request: it turns up while the clock is waiting in real time.
        seen.append(clock.now_ms())
        await clock.sleep_ms(10.0)
        seen.append(clock.now_ms())

    async def wait(_: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            task = asyncio.ensure_future(visitor())
            # Let it run up to its own sleep before the clock looks again.
            turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(turn.set_result, None)
            await turn
            assert not task.done()

    # One real slice of 20 ms is 20 simulated ms at 1x.
    clock = PacedSimClock(1.0, slice_s=0.02, wait=wait)
    await clock.sleep_ms(1000.0)

    # It arrived during the second slice, when 20 ms had been let through, and its own
    # 10 ms sleep ended at 30 ms exactly, not at the end of the 1000 ms gap.
    assert seen == [20.0, 30.0]
    assert clock.now_ms() == 1000.0


async def test_cancelled_sleepers_do_not_hold_the_clock_up() -> None:
    waits = Waits()
    clock = PacedSimClock(1.0, wait=waits)
    long_sleep = asyncio.ensure_future(clock.sleep_ms(60_000.0))

    await clock.sleep_ms(10.0)
    long_sleep.cancel()
    await asyncio.gather(long_sleep, return_exceptions=True)
    await clock.sleep_ms(10.0)

    assert clock.now_ms() == 20.0
    assert sum(waits.seconds) == pytest.approx(0.02)


async def test_many_short_gaps_are_paid_for_with_few_real_waits() -> None:
    # An operating system rounds a short sleep up, so the clock must not ask for one per
    # wake-up: a thousand 1 ms sleeps at 20x are 50 ms of real time in a handful of waits.
    waits = Waits()
    clock = PacedSimClock(20.0, min_wait_s=0.005, wait=waits)

    for _ in range(1000):
        await clock.sleep_ms(1.0)

    assert clock.now_ms() == 1000.0
    # Each wait is for the 5 ms that had built up (give or take one 0.05 ms sleep), and
    # what is still owed at the end is less than one wait.
    assert 9 <= len(waits.seconds) <= 10
    assert all(0.005 <= seconds < 0.0051 for seconds in waits.seconds)
    assert 0.045 < sum(waits.seconds) <= 0.05 + 1e-9


class SlowWaits:
    """A real clock on which every wait takes ``factor`` times as long as was asked."""

    def __init__(self, factor: float) -> None:
        self.factor = factor
        self.now_s = 0.0
        self.asked: list[float] = []

    async def wait(self, seconds: float) -> None:
        self.asked.append(seconds)
        self.now_s += seconds * self.factor

    def real(self) -> float:
        return self.now_s


async def test_a_wait_that_overran_is_made_up_by_waiting_less_afterwards() -> None:
    slow = SlowWaits(3.0)
    clock = PacedSimClock(1.0, slice_s=10.0, wait=slow.wait, real=slow.real)

    for _ in range(9):
        await clock.sleep_ms(10.0)

    # Each wait asked for 10 ms and took 30, which paid for the next two sleeps as well.
    assert slow.asked == pytest.approx([0.01, 0.01, 0.01])
    assert slow.now_s == pytest.approx(0.09)
    assert clock.now_ms() == 90.0


async def test_a_clock_that_fell_far_behind_does_not_rush_to_catch_up() -> None:
    slow = SlowWaits(1.0)
    clock = PacedSimClock(1.0, slice_s=10.0, max_behind_s=0.25, wait=slow.wait, real=slow.real)
    await clock.sleep_ms(100.0)

    # The machine stalls for a minute of real time with nothing sleeping.
    slow.now_s += 60.0
    for _ in range(10):
        await clock.sleep_ms(100.0)

    # Only a quarter of a second of the stall is made up: the first two sleeps and half
    # of the third are free, and the rest are waited for in full.
    assert sum(slow.asked) == pytest.approx(0.1 + 1.0 - 0.25)
    assert clock.now_ms() == 1100.0


def test_speed_and_slice_must_be_positive() -> None:
    with pytest.raises(ValueError, match="speed must be positive"):
        PacedSimClock(0.0)
    with pytest.raises(ValueError, match="slice_s must be positive"):
        PacedSimClock(1.0, slice_s=0.0)
    with pytest.raises(ValueError, match="must not be negative"):
        PacedSimClock(1.0, min_wait_s=-1.0)
    clock = PacedSimClock(5.0)
    with pytest.raises(ValueError, match="speed must be positive"):
        clock.speed = -1.0
    assert clock.speed == 5.0
