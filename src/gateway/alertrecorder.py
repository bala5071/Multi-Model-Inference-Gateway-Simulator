"""What happened to each application's requests, per minute: the numbers alert signals are
computed from (docs/43, the plan's section 6).

``app.py`` tells the recorder about each finished request of an application, as it tells
``Monitor``. The recorder never awaits and does no I/O: a call adds counts to a dictionary.
Its memory holds the applications seen since the counts were last taken, which the flusher
does every 10 s (``alertrollups.RollupFlusher``), or none at all when it writes straight into
the in-memory rollups.

Every count is a plain integer under a fixed field name, so the counts of several instances
for the same application and minute add up exactly, as Valkey's ``HINCRBY`` adds them. The two
latency histograms have fixed edges for the same reason: percentiles do not add, bucket counts
do. Nothing recorded is a prompt, a response, a token id, a key or a name: application ids,
minutes, status classes, fixed error codes and reasons, and counts.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping
from typing import Protocol

from gateway.clock import EpochSource

MINUTE_MS = 60_000.0

# The histogram's edges: 2^(k/4) ms for k = 0..71, from 1 ms to about 220 s, each about 19%
# above the last; a value goes to the first edge at or above it, a value above the last edge to
# the overflow bucket.
EDGES: tuple[float, ...] = tuple(2 ** (k / 4) for k in range(72))
OVERFLOW = len(EDGES)

# The error codes counted by name; any other is counted as "other", so the fields stay a
# fixed, small set whatever a client sends.
CODES = frozenset(
    {
        "state_unavailable",
        "rate_limited",
        "budget_exhausted",
        "queue_full",
        "request_timeout",
        "upstream_timeout",
        "upstream_unavailable",
        "upstream_rate_limited",
        "upstream_malformed",
        "upstream_rejected",
        "provider_auth_failed",
        "egress_refused",
        "tools_not_supported",
        "invalid_request",
        "internal_error",
    }
)
# The rejections the signals name (section 6): the code of each.
REJECTIONS = ("rate_limited", "budget_exhausted", "queue_full")
FALLBACK_REASONS = frozenset(
    {
        "connect_error",
        "upstream_5xx",
        "first_token_budget",
        "circuit_open",
        "invalid_tool_json",
        "key_unreadable",
        "attempt_cap",
    }
)
KINDS = ("generate", "chat")

Counts = dict[str, int]
MinuteKey = tuple[str, int]  # (application id, minute since the epoch)


def bucket(ms: float) -> int:
    """The histogram bucket of ``ms``: the index of the first edge at or above it."""
    return bisect.bisect_left(EDGES, ms)


def snap(ms: float) -> float:
    """The edge nearest ``ms``: what a latency threshold is stored as (decision 11)."""
    i = min(bucket(ms), OVERFLOW - 1)
    if i > 0 and ms - EDGES[i - 1] <= EDGES[i] - ms:
        return EDGES[i - 1]
    return EDGES[i]


def percentile(histogram: Mapping[int, int], p: float) -> float:
    """The nearest-rank ``p`` of the values counted in ``histogram`` (bucket to count), as the
    upper edge of the bucket it falls in; ``inf`` in the overflow bucket. Needs a count."""
    total = sum(histogram.values())
    rank = max(1, math.ceil(p * total))
    seen = 0
    for i in sorted(histogram):
        seen += histogram[i]
        if seen >= rank:
            return EDGES[i] if i < OVERFLOW else math.inf
    raise ValueError("an empty histogram has no percentile")


def status_class(status: int) -> str:
    return f"{status // 100}xx"


class RollupSink(Protocol):
    def absorb(self, deltas: Mapping[MinuteKey, Mapping[str, int]]) -> None:
        """Take counts at once, without I/O: the in-memory rollups."""
        ...


class AlertRecorder:
    """Per application and per minute of calendar time (``epoch``, comparable between
    instances): requests by kind, outcomes by status class and error code, rejections by
    reason, fallbacks by reason, hosted attempts, requests a hosted provider answered,
    downgrades, and the two latency histograms. With ``sink`` each request's counts go there
    at once; without, they wait for ``take``."""

    def __init__(self, epoch: EpochSource, sink: RollupSink | None = None) -> None:
        self._epoch = epoch
        self._sink = sink
        self._pending: dict[MinuteKey, Counts] = {}

    def record(
        self,
        application_id: str,
        *,
        kind: str,
        status: int,
        code: str | None = None,
        ttft_ms: float | None = None,
        total_ms: float | None = None,
        fallbacks: Iterable[str] = (),
        hosted_attempts: int = 0,
        hosted: bool = False,
        downgraded: bool = False,
    ) -> None:
        """One finished request of ``application_id``. Latencies only for an answered one."""
        if kind not in KINDS:
            raise ValueError(f"unknown request kind {kind!r}")
        key = (application_id, int(self._epoch.epoch_ms() // MINUTE_MS))
        counts: Counts = {} if self._sink is not None else self._pending.setdefault(key, {})

        def add(field: str, n: int = 1) -> None:
            counts[field] = counts.get(field, 0) + n

        add("requests")
        add(f"kind:{kind}")
        add(f"status:{status_class(status)}")
        if status == 504:
            add("status:504")
        if code is not None:
            add(f"code:{code if code in CODES else 'other'}")
            if code in REJECTIONS:
                add(f"reject:{code}")
        reasons = [r for r in fallbacks if r in FALLBACK_REASONS]
        if reasons:
            add("fallback:any")
            add(f"fallback:{reasons[0]}")  # the first, as X-Fallback-Reason reports it
        if hosted_attempts:
            add("hosted_attempts", hosted_attempts)
        if hosted:
            add("hosted_served")
        if downgraded:
            add("downgraded")
        if ttft_ms is not None:
            add(f"ttft:{bucket(ttft_ms)}")
        if total_ms is not None:
            add(f"total:{bucket(total_ms)}")
        if self._sink is not None:
            self._sink.absorb({key: counts})

    def take(self) -> dict[MinuteKey, Counts]:
        """Every count since the last take, and none kept: the deltas to flush."""
        taken, self._pending = self._pending, {}
        return taken

    def pending(self) -> int:
        """How many (application, minute) entries are waiting: the recorder's memory."""
        return len(self._pending)
