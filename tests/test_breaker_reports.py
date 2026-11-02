"""The breakers' reports off the request path (docs/41, scale-out PR 2b): in order for each
breaker, a bounded queue that drops and counts, the shutdown flush on the injected clock, a
half-open probe's report sent at once after its breaker's queued ones, and counters that cannot
answer counted as such."""

from __future__ import annotations

import asyncio

import pytest

from gateway.breaker import Admission, BreakerState
from gateway.breakerreports import BreakerReports, DropReason, Report
from gateway.clock import SimClock


class FakeStore:
    """Records every report; each write takes ``write_ms`` of the clock and answers
    ``written``."""

    def __init__(self, clock: SimClock, *, write_ms: float = 0.0, written: bool = True) -> None:
        self.clock = clock
        self.write_ms = write_ms
        self.written = written
        self.calls: list[tuple[str, str, str]] = []

    async def _record(self, op: str, organization_id: str, provider_id: str) -> bool:
        if self.write_ms:
            await self.clock.sleep_ms(self.write_ms)
        self.calls.append((op, organization_id, provider_id))
        return self.written

    async def admit(self, organization_id: str, provider_id: str) -> bool:
        return True

    async def admit_attempt(self, organization_id: str, provider_id: str) -> Admission:
        return Admission.ADMITTED

    async def release(self, organization_id: str, provider_id: str) -> bool:
        return await self._record("release", organization_id, provider_id)

    async def success(self, organization_id: str, provider_id: str) -> bool:
        return await self._record("success", organization_id, provider_id)

    async def failure(self, organization_id: str, provider_id: str) -> bool:
        return await self._record("failure", organization_id, provider_id)

    async def state(self, organization_id: str, provider_id: str) -> BreakerState:
        return BreakerState.CLOSED


def _reports(store: FakeStore, **kwargs: object) -> tuple[BreakerReports, list[DropReason]]:
    told: list[DropReason] = []
    return BreakerReports(store, store.clock, on_drop=told.append, **kwargs), told  # type: ignore[arg-type]


async def test_the_reports_are_sent_in_the_order_they_were_made() -> None:
    store = FakeStore(SimClock())
    reports, _ = _reports(store)
    made = [
        (Report.FAILURE, "north", "a"),
        (Report.SUCCESS, "north", "a"),
        (Report.FAILURE, "north", "b"),
        (Report.RELEASE, "north", "a"),
        (Report.FAILURE, "south", "a"),
    ]
    for report, org, provider in made:
        reports.enqueue(report, org, provider)
    assert store.calls == []  # nobody waited for them
    await reports.drained()
    assert store.calls == [(r.value, o, p) for r, o, p in made]


async def test_a_full_queue_drops_and_counts() -> None:
    store = FakeStore(SimClock())
    reports, told = _reports(store, size=2)
    for provider in ("a", "b", "c", "d"):
        reports.enqueue(Report.FAILURE, "north", provider)
    await reports.drained()
    assert [c[2] for c in store.calls] == ["a", "b"]
    assert reports.dropped[DropReason.QUEUE_FULL] == 2
    assert told == [DropReason.QUEUE_FULL] * 2


async def test_the_shutdown_flush_writes_what_is_queued_and_counts_what_it_cannot() -> None:
    clock = SimClock()
    store = FakeStore(clock, write_ms=900.0)
    reports, told = _reports(store)
    for provider in ("a", "b", "c", "d"):
        reports.enqueue(Report.SUCCESS, "north", provider)
    started = clock.now_ms()
    dropped = await reports.flush(2_000.0)
    # Two writes fit in the 2 s; the third is cut off while being written, the fourth never
    # starts: both are dropped and counted.
    assert [c[2] for c in store.calls] == ["a", "b"]
    assert dropped == 2
    assert reports.dropped[DropReason.FLUSH_TIMEOUT] == 2
    assert told == [DropReason.FLUSH_TIMEOUT] * 2
    assert clock.now_ms() - started == 2_000.0
    # After the flush nothing is queued: a late report is dropped, not left behind.
    reports.enqueue(Report.FAILURE, "north", "e")
    assert reports.queued == 0
    assert reports.dropped[DropReason.QUEUE_FULL] == 1


async def test_a_flush_with_time_enough_writes_everything_and_drops_nothing() -> None:
    clock = SimClock()
    store = FakeStore(clock, write_ms=100.0)
    reports, told = _reports(store)
    for provider in ("a", "b", "c"):
        reports.enqueue(Report.SUCCESS, "north", provider)
    assert await reports.flush(2_000.0) == 0
    assert [c[2] for c in store.calls] == ["a", "b", "c"]
    assert told == []


async def test_a_probe_report_is_sent_at_once_after_its_breakers_queued_ones() -> None:
    """The probe's report does not wait for the queue's sender, and the same breaker's reports
    still queued go before it, in order: the breaker sees every report as it was made. Another
    breaker's queued report is left to the sender."""
    store = FakeStore(SimClock())
    reports, _ = _reports(store)
    reports.enqueue(Report.FAILURE, "north", "a")
    reports.enqueue(Report.FAILURE, "north", "b")
    reports.enqueue(Report.RELEASE, "north", "a")
    await reports.send_now(Report.SUCCESS, "north", "a")
    assert store.calls == [
        ("failure", "north", "a"),
        ("release", "north", "a"),
        ("success", "north", "a"),
    ]
    await reports.drained()
    assert store.calls[-1] == ("failure", "north", "b")


async def test_a_report_the_counters_cannot_take_is_counted() -> None:
    store = FakeStore(SimClock(), written=False)
    reports, told = _reports(store)
    reports.enqueue(Report.FAILURE, "north", "a")
    await reports.drained()
    await reports.send_now(Report.SUCCESS, "north", "a")
    assert reports.dropped[DropReason.UNAVAILABLE] == 2
    assert told == [DropReason.UNAVAILABLE] * 2


def test_the_queue_needs_room() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BreakerReports(FakeStore(SimClock()), SimClock(), size=0)


async def test_enqueueing_never_waits() -> None:
    """Enqueueing is not a coroutine: a request cannot wait on it, however slow the store."""
    store = FakeStore(SimClock(), write_ms=10_000.0)
    reports, _ = _reports(store)
    assert not asyncio.iscoroutinefunction(reports.enqueue)
    reports.enqueue(Report.FAILURE, "north", "a")
    assert reports.queued == 1
    await reports.flush(0.0)
