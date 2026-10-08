from __future__ import annotations

import asyncio
from typing import cast

import pytest

from gateway.clock import Clock, SimClock, WallClock, _ready_queue


async def test_sim_clock_starts_at_the_given_time() -> None:
    assert SimClock().now_ms() == 0.0
    assert SimClock(start_ms=250.0).now_ms() == 250.0


async def test_sleep_advances_time_by_exactly_the_duration() -> None:
    clock = SimClock()
    await clock.sleep_ms(12.5)
    assert clock.now_ms() == 12.5
    await clock.sleep_ms(0.25)
    assert clock.now_ms() == 12.75


async def test_zero_sleep_does_not_move_time() -> None:
    clock = SimClock(start_ms=3.0)
    await clock.sleep_ms(0)
    assert clock.now_ms() == 3.0


async def test_concurrent_sleepers_wake_in_time_order_and_do_not_add_up() -> None:
    clock = SimClock()
    woken: list[tuple[float, float]] = []

    async def sleeper(duration_ms: float) -> None:
        await clock.sleep_ms(duration_ms)
        woken.append((duration_ms, clock.now_ms()))

    await asyncio.gather(sleeper(10), sleeper(5), sleeper(7))

    assert woken == [(5, 5), (7, 7), (10, 10)]
    assert clock.now_ms() == 10


async def test_sleepers_due_at_the_same_instant_wake_in_sleep_order() -> None:
    clock = SimClock()
    woken: list[str] = []

    async def sleeper(name: str) -> None:
        await clock.sleep_ms(5)
        woken.append(name)

    await asyncio.gather(sleeper("a"), sleeper("b"), sleeper("c"))

    assert woken == ["a", "b", "c"]


async def test_time_waits_for_a_long_chain_of_tasks_to_settle() -> None:
    # A timer at t=1 starts a relay through 200 tasks; the last one sleeps 1 ms and must
    # wake at t=2. If the clock jumped to the t=5 timer before the relay finished, the
    # last task would wake at t=6 instead.
    clock = SimClock()
    relay_length = 200
    events = [asyncio.Event() for _ in range(relay_length)]
    woke_at: list[float] = []

    async def kick_off() -> None:
        await clock.sleep_ms(1)
        events[0].set()

    async def relay(i: int) -> None:
        await events[i].wait()
        if i + 1 < relay_length:
            events[i + 1].set()
        else:
            await clock.sleep_ms(1)
            woke_at.append(clock.now_ms())

    await asyncio.gather(
        clock.sleep_ms(5), kick_off(), *(relay(i) for i in reversed(range(relay_length)))
    )

    assert woke_at == [2]
    assert clock.now_ms() == 5


async def test_cancelled_sleeper_does_not_move_time() -> None:
    clock = SimClock()
    long_sleep = asyncio.create_task(clock.sleep_ms(100))
    await clock.sleep_ms(1)
    long_sleep.cancel()
    await clock.sleep_ms(4)
    with pytest.raises(asyncio.CancelledError):
        await long_sleep

    await clock.sleep_ms(0)
    assert clock.now_ms() == 5


async def test_clock_with_only_cancelled_sleepers_does_not_stall_another_clock() -> None:
    # Regression: the first clock used to keep yielding while it still held the cancelled
    # timer, so the loop never looked idle to the second clock and both spun forever.
    first = SimClock()
    abandoned = asyncio.create_task(first.sleep_ms(1000))
    await first.sleep_ms(1)
    abandoned.cancel()

    second = SimClock()
    await second.sleep_ms(5)

    assert second.now_ms() == 5
    assert first.now_ms() == 1
    assert abandoned.cancelled()


@pytest.mark.parametrize("clock", [SimClock(), WallClock()], ids=["sim", "wall"])
async def test_negative_sleep_is_rejected(clock: Clock) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        await clock.sleep_ms(-1)


def test_sim_clock_refuses_an_event_loop_it_cannot_inspect() -> None:
    with pytest.raises(RuntimeError, match="standard asyncio event loop"):
        _ready_queue(cast(asyncio.AbstractEventLoop, object()))


async def test_wall_clock_does_not_go_backwards() -> None:
    clock = WallClock()
    before = clock.now_ms()
    await clock.sleep_ms(0)
    assert clock.now_ms() >= before
