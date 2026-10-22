"""Budget periods: which window of epoch time a moment belongs to.

See docs/21-persistence.md. The default is the calendar month in UTC. Periods are computed
from the injected epoch source's readings, never from the system clock.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Protocol

DAY_MS = 86_400_000
_EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()


class BudgetPeriods(Protocol):
    def bounds(self, at_ms: float) -> tuple[float, float]:
        """The period containing ``at_ms``: ``start <= at_ms < end``, in epoch ms."""
        ...


def _date_ms(day: datetime.date) -> float:
    return float((day.toordinal() - _EPOCH_ORDINAL) * DAY_MS)


class CalendarMonthsUTC:
    """Calendar months in UTC: a period starts at 00:00 UTC on the 1st.

    Worked out from whole days since the epoch (``floor(at_ms / day)``), not from
    ``datetime.fromtimestamp``, which rounds to microseconds and could put the last instant
    of a month into the next one.
    """

    def bounds(self, at_ms: float) -> tuple[float, float]:
        day = datetime.date.fromordinal(_EPOCH_ORDINAL + math.floor(at_ms / DAY_MS))
        start = day.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return _date_ms(start), _date_ms(end)


@dataclass(frozen=True, slots=True)
class FixedPeriods:
    """Fixed windows of ``length_ms`` counted from ``start_ms``: for tests that want short,
    hand-checkable periods."""

    start_ms: float
    length_ms: float

    def __post_init__(self) -> None:
        if self.length_ms <= 0:
            raise ValueError("length_ms must be positive")

    def bounds(self, at_ms: float) -> tuple[float, float]:
        index = (at_ms - self.start_ms) // self.length_ms
        start = self.start_ms + index * self.length_ms
        return start, start + self.length_ms
