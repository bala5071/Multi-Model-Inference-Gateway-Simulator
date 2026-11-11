"""The alert signals and their minimums (docs/43, the plan's section 6).

Every signal a customer can be alerted on is computed only from that application's own
rollups (``MinuteRollup``) or from durable values of its own (its budget, its organization's
providers, its keys); nothing here takes a model's or another application's number. Each
signal has a minimum: below it the value is "too few to judge", never 0, and a window with an
incomplete minute is too few to judge as well. Pure functions: no store, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gateway.alertrecorder import FALLBACK_REASONS, OVERFLOW, REJECTIONS, percentile
from gateway.alertrollups import MinuteRollup

# The minimum for a rate: requests in the window.
RATE_MINIMUM = 20


def percentile_minimum(p: float) -> int:
    """The requests that put at least five above the ``p`` percentile: ``5 / (1 - p)``."""
    return math.ceil(round(5 / (1 - p), 9))


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    unit: str
    windows: tuple[int, ...]  # minutes; () for "now" or "the period so far"
    # The parameter's allowed values, or () when the signal takes none.
    parameters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SignalValue:
    """A signal's value, or why there is none: ``too_few`` (below its minimum),
    ``incomplete`` (a minute of the window lost counts), ``unavailable`` (a store that could
    not answer), ``no_budget`` (an effective budget of 0)."""

    value: float | None
    why_not: str | None = None

    @property
    def judgeable(self) -> bool:
        return self.why_not is None


TOO_FEW = SignalValue(None, "too_few")
INCOMPLETE = SignalValue(None, "incomplete")

_WINDOWS = (5, 15, 60)
_FALLBACKS = ("any", *sorted(FALLBACK_REASONS))
_PERCENTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}

SIGNALS: Mapping[str, Signal] = {
    s.name: s
    for s in (
        *(Signal(f"latency_total_{p}", "ms", _WINDOWS) for p in _PERCENTILES),
        *(Signal(f"latency_ttft_{p}", "ms", _WINDOWS) for p in _PERCENTILES),
        Signal("error_rate", "%", _WINDOWS),
        Signal("timeout_rate", "%", _WINDOWS),
        Signal("state_unavailable_rate", "%", (5, 15)),
        Signal("rejections", "count", _WINDOWS, REJECTIONS),
        Signal("rejection_rate", "%", _WINDOWS, REJECTIONS),
        Signal("fallback_rate", "%", (15, 60), _FALLBACKS),
        Signal("hosted_share", "%", (15, 60)),
        Signal("downgrade_rate", "%", (15, 60)),
        Signal("requests", "count", _WINDOWS),
        Signal("budget_used", "%", ()),
        Signal("circuit_open", "min", ()),
        Signal("key_expires_in", "days", ()),
        Signal("reservation_expired", "count", (60,)),
    )
}
# The signals computed from the rollups; the others from durable values (``budget_used`` etc.).
FROM_ROLLUPS = frozenset(n for n in SIGNALS if n not in {
    "budget_used", "circuit_open", "key_expires_in", "reservation_expired"
})  # fmt: skip


def _summed(rollups: Sequence[MinuteRollup]) -> dict[str, int]:
    total: dict[str, int] = {}
    for rollup in rollups:
        for name, n in rollup.counts.items():
            total[name] = total.get(name, 0) + n
    return total


def _histogram(counts: Mapping[str, int], kind: str) -> dict[int, int]:
    found: dict[int, int] = {}
    for i in range(OVERFLOW + 1):
        n = counts.get(f"{kind}:{i}", 0)
        if n:
            found[i] = n
    return found


def _rate(numerator: int, denominator: int) -> SignalValue:
    if denominator < RATE_MINIMUM:
        return TOO_FEW
    return SignalValue(100.0 * numerator / denominator)


def _check(
    name: str, application_id: str, rollups: Sequence[MinuteRollup], window: int,
    parameter: str | None,
) -> None:  # fmt: skip
    """Refuse a signal that is not computed from rollups, a window or parameter it does not
    take, and rollups that are not ``window`` consecutive minutes of ``application_id``."""
    signal = SIGNALS.get(name)
    if signal is None or name not in FROM_ROLLUPS:
        raise ValueError(f"{name!r} is not a signal computed from rollups")
    if window not in signal.windows:
        raise ValueError(f"{name} has no {window}-minute window")
    if (parameter is None) != (not signal.parameters) or (
        parameter is not None and parameter not in signal.parameters
    ):
        raise ValueError(f"{name} does not take the parameter {parameter!r}")
    minutes = [r.minute for r in rollups]
    if len(rollups) != window or minutes != list(range(minutes[0], minutes[0] + window)):
        raise ValueError(f"{name} needs {window} consecutive minutes")
    if any(r.application_id != application_id for r in rollups):
        raise ValueError("a signal is computed from one application's rollups only")


def _value(name: str, parameter: str | None, counts: Mapping[str, int]) -> SignalValue:
    """``name`` from a complete window's summed counts."""
    requests = counts.get("requests", 0)
    if name.startswith("latency_"):
        kind = "total" if name.startswith("latency_total_") else "ttft"
        p = _PERCENTILES[name.rsplit("_", 1)[1]]
        histogram = _histogram(counts, kind)
        if sum(histogram.values()) < percentile_minimum(p):
            return TOO_FEW
        return SignalValue(percentile(histogram, p))
    if name == "error_rate":
        return _rate(counts.get("status:5xx", 0), requests)
    if name == "timeout_rate":
        return _rate(counts.get("status:504", 0), requests)
    if name == "state_unavailable_rate":
        return _rate(counts.get("code:state_unavailable", 0), requests)
    if name == "rejections":
        return SignalValue(float(counts.get(f"reject:{parameter}", 0)))
    if name == "rejection_rate":
        return _rate(counts.get(f"reject:{parameter}", 0), requests)
    if name == "fallback_rate":
        return _rate(counts.get(f"fallback:{parameter}", 0), counts.get("kind:chat", 0))
    if name == "hosted_share":
        return _rate(counts.get("hosted_served", 0), counts.get("kind:chat", 0))
    if name == "downgrade_rate":
        return _rate(counts.get("downgraded", 0), counts.get("kind:generate", 0))
    assert name == "requests"
    return SignalValue(float(requests))


def evaluate(
    name: str,
    application_id: str,
    rollups: Sequence[MinuteRollup],
    *,
    window: int,
    parameter: str | None = None,
) -> SignalValue:
    """``name`` over ``rollups``, which must be ``window`` consecutive minutes of
    ``application_id`` and nothing else."""
    _check(name, application_id, rollups, window, parameter)
    if any(r.incomplete for r in rollups):
        return INCOMPLETE
    return _value(name, parameter, _summed(rollups))


def evaluate_many(
    names: Sequence[str], application_id: str, rollups: Sequence[MinuteRollup], *, window: int
) -> dict[str, SignalValue]:
    """``evaluate`` of each of ``names`` (none taking a parameter) over the same rollups, the
    window checked and summed once: the same values, for a page of applications (docs/51)."""
    for name in names:
        _check(name, application_id, rollups, window, None)
    if any(r.incomplete for r in rollups):
        return dict.fromkeys(names, INCOMPLETE)
    counts = _summed(rollups)
    return {name: _value(name, None, counts) for name in names}


# --- the durable signals: values the evaluator reads from the stores (A3) -----------------------


def budget_used(committed_tokens: int, budget_tokens: int) -> SignalValue:
    """% of the effective monthly budget committed this period; none with a budget of 0."""
    if budget_tokens <= 0:
        return SignalValue(None, "no_budget")
    return SignalValue(100.0 * committed_tokens / budget_tokens)


def circuit_open(minutes: float | None) -> SignalValue:
    """Minutes a provider's breaker has been open or half-open; None: the store did not answer."""
    if minutes is None:
        return SignalValue(None, "unavailable")
    return SignalValue(minutes)


def key_expires_in(days: float) -> SignalValue:
    return SignalValue(days)


def reservation_expired(count: int, *, incomplete: bool = False) -> SignalValue:
    return INCOMPLETE if incomplete else SignalValue(float(count))
