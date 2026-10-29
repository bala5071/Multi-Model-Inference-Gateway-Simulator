"""The circuit breaker and the slow-provider warning, on the injected clock. docs/33."""

from __future__ import annotations

from gateway.breaker import Breakers, BreakerState, Transition
from gateway.clock import SimClock

ORG, PROVIDER = "north", "prv_00000000000000a1"


def make() -> tuple[Breakers, SimClock, list[Transition]]:
    clock = SimClock()
    moves: list[Transition] = []
    return Breakers(clock, moves.append), clock, moves


async def fail(breakers: Breakers, times: int) -> None:
    for _ in range(times):
        assert await breakers.admit(ORG, PROVIDER)
        await breakers.failure(ORG, PROVIDER)


async def test_five_consecutive_failures_open_it_and_a_success_resets_the_count() -> None:
    breakers, _, moves = make()
    await fail(breakers, 4)
    await breakers.success(ORG, PROVIDER)
    await fail(breakers, 4)
    assert await breakers.state(ORG, PROVIDER) is BreakerState.CLOSED
    await fail(breakers, 1)
    assert await breakers.state(ORG, PROVIDER) is BreakerState.OPEN
    assert not await breakers.admit(ORG, PROVIDER)
    assert moves == [Transition(ORG, PROVIDER, BreakerState.OPEN, 30_000.0)]


async def test_half_open_admits_one_probe_and_two_successes_close_it() -> None:
    breakers, clock, moves = make()
    await fail(breakers, 5)
    await clock.sleep_ms(29_999.0)
    assert not await breakers.admit(ORG, PROVIDER)
    await clock.sleep_ms(1.0)
    assert await breakers.admit(ORG, PROVIDER)  # the probe
    assert not await breakers.admit(ORG, PROVIDER)  # only one at a time
    await breakers.success(ORG, PROVIDER)
    assert await breakers.state(ORG, PROVIDER) is BreakerState.HALF_OPEN  # one is not enough
    assert await breakers.admit(ORG, PROVIDER)
    await breakers.success(ORG, PROVIDER)
    assert await breakers.state(ORG, PROVIDER) is BreakerState.CLOSED
    assert [m.state for m in moves] == [
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
        BreakerState.CLOSED,
    ]


async def test_a_failed_probe_doubles_the_open_time_up_to_five_minutes() -> None:
    breakers, clock, moves = make()
    await fail(breakers, 5)
    for _ in range(6):
        await clock.sleep_ms(moves[-1].open_ms or 0.0)
        assert await breakers.admit(ORG, PROVIDER)
        await breakers.failure(ORG, PROVIDER)
    opened = [m.open_ms for m in moves if m.state is BreakerState.OPEN]
    assert opened == [30_000.0, 60_000.0, 120_000.0, 240_000.0, 300_000.0, 300_000.0, 300_000.0]


async def test_a_probe_without_a_verdict_frees_the_slot() -> None:
    breakers, clock, _ = make()
    await fail(breakers, 5)
    await clock.sleep_ms(30_000.0)
    assert await breakers.admit(ORG, PROVIDER)
    await breakers.release(ORG, PROVIDER)  # cancelled, or a failure that is not a trigger
    assert await breakers.admit(ORG, PROVIDER)


async def test_breakers_are_per_organization_and_provider() -> None:
    breakers, _, _ = make()
    await fail(breakers, 5)
    assert not await breakers.admit(ORG, PROVIDER)
    assert await breakers.admit("south", PROVIDER)
    assert await breakers.admit(ORG, "prv_00000000000000b2")


async def test_three_missed_budgets_flag_a_slow_provider_and_one_in_budget_clears_it() -> None:
    breakers, clock, _ = make()
    await clock.sleep_ms(5.0)
    for _ in range(2):
        breakers.first_token(ORG, PROVIDER, missed=True)
    assert breakers.slow(ORG, PROVIDER).slow_since_ms is None
    breakers.first_token(ORG, PROVIDER, missed=True)
    warning = breakers.slow(ORG, PROVIDER)
    assert (warning.slow_since_ms, warning.consecutive_misses) == (5.0, 3)
    breakers.first_token(ORG, PROVIDER, missed=True)
    assert breakers.slow(ORG, PROVIDER).slow_since_ms == 5.0  # since the third, not later
    breakers.first_token(ORG, PROVIDER, missed=False)
    cleared = breakers.slow(ORG, PROVIDER)
    assert (cleared.slow_since_ms, cleared.consecutive_misses) == (None, 0)
