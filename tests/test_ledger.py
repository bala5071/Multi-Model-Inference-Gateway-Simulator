from __future__ import annotations

import pytest

from gateway.clock import SimClock
from gateway.ledger import BudgetExceededError, CostLedger, ModelUsage
from gateway.periods import FixedPeriods
from gateway.schema import RequestResult

# January 1970: epoch time 0 is where a SimClock's ClockEpoch starts.
JANUARY_1970_MS = 31 * 24 * 60 * 60 * 1000.0


def _result(
    tokens_in: int, tokens_out: int, cost_usd: float, cached_tokens: int = 0
) -> RequestResult:
    return RequestResult(
        request_id="r",
        model="m",
        enqueue_ms=0.0,
        start_ms=0.0,
        first_token_ms=1.0,
        finish_ms=2.0,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        cached_tokens=cached_tokens,
    )


def test_new_tenant_has_no_usage() -> None:
    usage = CostLedger(SimClock()).usage("t1")
    assert usage.total == ModelUsage()
    assert usage.by_model == {}
    assert (usage.period_start_ms, usage.period_end_ms) == (0.0, JANUARY_1970_MS)


def test_record_adds_tokens_and_cost_and_returns_the_billed_amount() -> None:
    ledger = CostLedger(SimClock())

    first = ledger.record("t1", _result(100, 20, 0.5), model="economy", price_multiplier=0.8)
    second = ledger.record(
        "t1", _result(60, 10, 0.25, cached_tokens=40), model="premium", price_multiplier=0.8
    )

    assert (first, second) == (0.5 * 0.8, 0.25 * 0.8)
    usage = ledger.usage("t1")
    assert usage.total == ModelUsage(
        requests=2,
        prompt_tokens=160,
        cached_tokens=40,
        completion_tokens=30,
        base_cost_usd=0.75,
        billed_cost_usd=0.5 * 0.8 + 0.25 * 0.8,
    )
    # Cached prompt tokens are not billed, so they do not count against the budget.
    assert usage.total.billed_tokens == 160 - 40 + 30
    assert usage.by_model == {
        "economy": ModelUsage(1, 100, 0, 20, 0.5, 0.5 * 0.8),
        "premium": ModelUsage(1, 60, 40, 10, 0.25, 0.25 * 0.8),
    }


def test_zero_multiplier_tracks_tokens_and_base_cost_but_bills_nothing() -> None:
    ledger = CostLedger(SimClock())
    assert ledger.record("t1", _result(10, 5, 0.3), model="economy", price_multiplier=0.0) == 0.0
    total = ledger.usage("t1").total
    assert (total.billed_tokens, total.base_cost_usd, total.billed_cost_usd) == (15, 0.3, 0.0)


def test_tenants_are_accounted_separately() -> None:
    ledger = CostLedger(SimClock())
    ledger.record("t1", _result(10, 5, 0.1), model="economy", price_multiplier=1.0)
    ledger.record("t2", _result(7, 3, 0.2), model="economy", price_multiplier=1.0)

    assert ledger.usage("t1").total.billed_tokens == 15
    assert ledger.usage("t2").total.billed_tokens == 10
    assert ledger.usage("t3").total.billed_tokens == 0


def test_budget_check_passes_until_the_budget_is_used_up() -> None:
    ledger = CostLedger(SimClock())
    ledger.check_budget("t1", 30)
    ledger.record("t1", _result(10, 5, 0.1), model="economy", price_multiplier=1.0)
    ledger.check_budget("t1", 30)  # 15 of 30
    ledger.record("t1", _result(10, 5, 0.1), model="economy", price_multiplier=1.0)

    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.check_budget("t1", 30)  # 30 of 30

    error = excinfo.value
    assert (error.tenant_id, error.used_tokens, error.budget_tokens) == ("t1", 30, 30)
    assert error.retry_after_ms == JANUARY_1970_MS
    assert "used 30 of its 30 token budget" in str(error)
    ledger.check_budget("t1", 31)
    ledger.check_budget("t2", 30)  # another tenant is unaffected


def test_zero_budget_is_exhausted_from_the_start() -> None:
    with pytest.raises(BudgetExceededError):
        CostLedger(SimClock()).check_budget("t1", 0)


async def test_usage_resets_when_the_period_rolls_over() -> None:
    clock = SimClock(start_ms=500.0)
    ledger = CostLedger(clock, periods=FixedPeriods(500.0, 1000.0))
    ledger.record("t1", _result(10, 5, 0.1), model="economy", price_multiplier=1.0)

    await clock.sleep_ms(999.0)  # still inside the first period, which is 500..1500
    usage = ledger.usage("t1")
    assert usage.total.billed_tokens == 15
    assert (usage.period_start_ms, usage.period_end_ms) == (500.0, 1500.0)
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.check_budget("t1", 15)
    assert excinfo.value.retry_after_ms == 1.0

    await clock.sleep_ms(1.0)  # the second period begins exactly at 1500
    usage = ledger.usage("t1")
    assert usage.total == ModelUsage()
    assert usage.by_model == {}
    assert (usage.period_start_ms, usage.period_end_ms) == (1500.0, 2500.0)
    ledger.check_budget("t1", 15)


async def test_a_tenant_first_seen_in_a_later_period_gets_that_period() -> None:
    clock = SimClock()
    ledger = CostLedger(clock, periods=FixedPeriods(0.0, 1000.0))
    await clock.sleep_ms(2500.0)
    usage = ledger.usage("late")
    assert (usage.period_start_ms, usage.period_end_ms) == (2000.0, 3000.0)


def test_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="length_ms"):
        FixedPeriods(0.0, 0.0)
