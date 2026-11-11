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
from collections.abc import Sequence
from dataclasses import dataclass

from gateway.alertrecorder import MINUTE_MS
from gateway.alertrollups import MinuteRollup
from gateway.alertrules import COMPLETE_AFTER_MS
from gateway.signals import SignalValue, evaluate_many

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
