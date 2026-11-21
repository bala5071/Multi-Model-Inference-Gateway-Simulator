"""An application's figures for the console's Applications list (docs/51).

The figures are the alert signals' own (``signals.evaluate_many``, ``evaluate``'s own code) over
the window the evaluator uses: the last complete minutes, a minute being complete 25 s after it
ends. So a figure on the
list and the value an alert rule judges never disagree, and each has the same minimum: below it
the figure is "too few to judge", never 0; a window with a minute that lost counts is
"incomplete"; rollups that could not be read are "unavailable".

Pure: no store, no clock. What comes out is ids, minutes, fixed names and numbers: never a
prompt, a response, a key or a text.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gateway.alertrecorder import HOUR_MS, MINUTE_MS
from gateway.alertrollups import MinuteRollup
from gateway.alertrules import COMPLETE_AFTER_MS
from gateway.incidents import OPEN_STATES
from gateway.signals import SignalValue, evaluate_counts, evaluate_many

# The windows the console may ask for, by name, in minutes. 24 hours comes with the recorder's
# hour keys (docs/console-ia-v2-plan.md, section 3).
WINDOWS = {"60m": 60}
# The figures of one application, each a signal of ``signals.SIGNALS``.
FIGURES = ("requests", "error_rate", "latency_total_p50")
# Why a figure has no value, beyond the signals' own: rollups that could not be read, and a
# percentile above the histogram's last edge (JSON has no infinity).
UNAVAILABLE = "unavailable"
ABOVE_RANGE = "above_range"


@dataclass(frozen=True, slots=True)
class Window:
    minutes: int
    first_minute: int
    last_minute: int
    # When the rollups are this process's own and it started inside the window: from when they
    # hold counts. None when the window is whole.
    since_ms: float | None = None

    @property
    def minute_range(self) -> range:
        return range(self.first_minute, self.last_minute + 1)


def window_for(now_ms: float, minutes: int, started_ms: float | None = None) -> Window:
    """The ``minutes`` complete minutes before ``now_ms`` (epoch time), as the evaluator takes
    them; ``started_ms``: when in-memory rollups began, or None for rollups that outlive the
    process."""
    last = int((now_ms - COMPLETE_AFTER_MS) // MINUTE_MS) - 1
    first = last - minutes + 1
    since = started_ms if started_ms is not None and started_ms > first * MINUTE_MS else None
    return Window(minutes, first, last, since)


def window_view(window: Window) -> dict[str, object]:
    return {
        "minutes": window.minutes,
        "first_minute_ms": window.first_minute * MINUTE_MS,
        "end_ms": (window.last_minute + 1) * MINUTE_MS,
        "since_ms": window.since_ms,
    }


def _figure(found: SignalValue) -> dict[str, object]:
    if found.value is not None and math.isinf(found.value):
        return {"value": None, "why_not": ABOVE_RANGE}
    return {"value": found.value, "why_not": found.why_not}


def figures(
    application_id: str, rollups: Sequence[MinuteRollup], window: Window
) -> dict[str, dict[str, object]]:
    """Each figure of ``FIGURES`` over ``rollups``, which are ``window``'s minutes of
    ``application_id``, in order."""
    found = evaluate_many(FIGURES, application_id, rollups, window=window.minutes)
    return {name: _figure(found[name]) for name in FIGURES}


def unavailable() -> dict[str, dict[str, object]]:
    """Every figure, when the rollups could not be read."""
    return {name: {"value": None, "why_not": UNAVAILABLE} for name in FIGURES}


# --- The platform owner's figures: Clients and one client's applications (docs/57) -------------

# The windows the Clients pages may ask for: how many buckets, and how long each is. 60 minutes
# from the minute keys, 24 hours from the recorder's hour keys (kept 26 hours).
CLIENT_WINDOWS: dict[str, tuple[int, int]] = {
    "60m": (60, int(MINUTE_MS)),
    "24h": (24, int(HOUR_MS)),
}
# Each client figure: its name in the answer, and the signal and parameter it is.
CLIENT_FIGURES: tuple[tuple[str, str | None], ...] = (
    ("requests", None),
    ("error_rate", None),
    ("fallback_rate", "any"),
    ("latency_total_p50", None),
)
# An organization's row on Clients carries no percentile (docs/console-ia-v2-plan.md 15.1).
ORGANIZATION_FIGURES = tuple(f for f in CLIENT_FIGURES if f[0] != "latency_total_p50")
# Why a window starts later than it says: this process's in-memory counts began when it started,
# or the shared store began counting this kind then. Each is said on the page (15.6, 3).
RESTARTED = "restarted"
COUNTING_BEGAN = "counting_began"

# The platform's totals sum at most this many organizations' buckets (read a page of 100 at a
# time); past it they are not counted, and say so (15.6, condition 4).
TOTALS_CAP = 1000
ABOVE_CAP = "above_cap"

# The field names each answer may hold, and nothing else (section 13: a test compares them).
ORGANIZATION_LIST_FIELDS = frozenset({"organization_id", "name", "kind", "enabled", "applications"})
ORGANIZATION_USAGE_FIELDS = frozenset(
    {"organization_id", "open_incidents", *(n for n, _ in ORGANIZATION_FIGURES)}
)
PLATFORM_TOTAL_FIELDS = frozenset({"open_incidents", *(n for n, _ in ORGANIZATION_FIGURES)})
CLIENT_APPLICATION_FIELDS = frozenset(
    {"application_id", "name", "open_incidents", *(n for n, _ in CLIENT_FIGURES)}
)
CLIENT_DETAIL_FIELDS = frozenset({"application_id", "name", "window", "open_incidents", "buckets"})
CLIENT_BUCKET_FIELDS = frozenset({"start_ms", *(n for n, _ in CLIENT_FIGURES)})
SPAN_FIELDS = frozenset({"name", "bucket_ms", "first_ms", "end_ms", "since_ms", "since_reason"})
OPEN_INCIDENT_FIELDS = frozenset(state.value for state in OPEN_STATES)


@dataclass(frozen=True, slots=True)
class Span:
    """A Clients window: ``first`` to ``last`` buckets of ``bucket_ms`` (minute or hour numbers
    since the epoch), the last complete one last; and, when the counts begin inside it, from
    when and why (``RESTARTED`` or ``COUNTING_BEGAN``)."""

    name: str
    bucket_ms: int
    first: int
    last: int
    since_ms: float | None = None
    since_reason: str | None = None

    @property
    def buckets(self) -> range:
        return range(self.first, self.last + 1)


def span_for(
    name: str,
    now_ms: float,
    started_ms: float | None = None,
    counted_since_minute: int | None = None,
) -> Span:
    """``name``'s window before ``now_ms`` (epoch time), its buckets complete as the evaluator's
    minutes are (``COMPLETE_AFTER_MS`` after they end). ``started_ms``: when this process's
    in-memory rollups began (None for a shared store); ``counted_since_minute``: the minute since
    which a shared store has counted the kind read (``AlertRollups.read_extras``)."""
    count, bucket_ms = CLIENT_WINDOWS[name]
    last = int((now_ms - COMPLETE_AFTER_MS) // bucket_ms) - 1
    first = last - count + 1
    start_ms = first * bucket_ms
    if started_ms is not None and started_ms > start_ms:
        return Span(name, bucket_ms, first, last, started_ms, RESTARTED)
    if counted_since_minute is not None and counted_since_minute * MINUTE_MS > start_ms:
        return Span(name, bucket_ms, first, last, counted_since_minute * MINUTE_MS, COUNTING_BEGAN)
    return Span(name, bucket_ms, first, last)


def span_view(span: Span) -> dict[str, object]:
    return {
        "name": span.name,
        "bucket_ms": span.bucket_ms,
        "first_ms": span.first * span.bucket_ms,
        "end_ms": (span.last + 1) * span.bucket_ms,
        "since_ms": span.since_ms,
        "since_reason": span.since_reason,
    }


def client_figures(
    rollups: Sequence[MinuteRollup],
    wanted: Sequence[tuple[str, str | None]] = CLIENT_FIGURES,
) -> dict[str, dict[str, object]]:
    """Each of ``wanted`` over ``rollups`` summed (one subject's buckets, or several's)."""
    found = evaluate_counts(wanted, rollups)
    return {name: _figure(found[name]) for name, _ in wanted}


def client_unavailable(
    wanted: Sequence[tuple[str, str | None]] = CLIENT_FIGURES, why_not: str = UNAVAILABLE
) -> dict[str, dict[str, object]]:
    """Each of ``wanted`` with no value: the rollups could not be read, or (``ABOVE_CAP``) the
    platform has more organizations than its totals count."""
    return {name: {"value": None, "why_not": why_not} for name, _ in wanted}


def client_buckets(rollups: Sequence[MinuteRollup], span: Span) -> list[dict[str, object]]:
    """One row per bucket of ``span``, in order: when it starts and its figures alone."""
    return [{"start_ms": r.minute * span.bucket_ms, **client_figures([r])} for r in rollups]


def open_incidents(per_state: Mapping[str, int] | None) -> dict[str, int]:
    """The open incident counts by state (Q2: counts only), every open state named."""
    return {state.value: (per_state or {}).get(state.value, 0) for state in OPEN_STATES}


def summed_open(counts: Mapping[str, Mapping[str, int]]) -> dict[str, int]:
    """An organization's open incidents by state, from ``open_counts``' per application answer."""
    total = open_incidents(None)
    for per_state in counts.values():
        for state, n in open_incidents(per_state).items():
            total[state] += n
    return total
