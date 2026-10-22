"""The in-process event bus and its bounded log."""

from __future__ import annotations

import pytest

from gateway.clock import SimClock
from gateway.events import EVENT_TYPES, Event, EventBus


async def test_publish_stamps_the_clock_and_numbers_events_from_one() -> None:
    clock = SimClock()
    bus = EventBus(clock)

    first = bus.publish("queue_full", model="premium", tenant_id="acme", waiting=2, limit=2)
    await clock.sleep_ms(250.0)
    second = bus.publish("state_change", model="premium", degraded=True, p99_ms=31.5)

    assert first == Event(1, 0.0, "queue_full", "premium", "acme", {"waiting": 2, "limit": 2})
    assert second == Event(
        2, 250.0, "state_change", "premium", None, {"degraded": True, "p99_ms": 31.5}
    )
    assert bus.last_seq == 2
    assert bus.recent(10) == (first, second)
    assert bus.recent(1) == (second,)
    assert bus.recent(0) == ()


def test_counts_list_every_type_and_only_known_types_are_accepted() -> None:
    bus = EventBus(SimClock())
    bus.publish("timeout", model="m")
    bus.publish("timeout", model="m")
    bus.publish("downgrade", model="m", to_model="n")

    assert bus.counts == {
        "state_change": 0,
        "queue_full": 0,
        "timeout": 2,
        "downgrade": 1,
        "budget_exhausted": 0,
    }
    assert tuple(bus.counts) == EVENT_TYPES
    with pytest.raises(ValueError, match="unknown event type 'surprise'"):
        bus.publish("surprise")
    assert bus.last_seq == 3


def test_since_returns_what_is_newer_than_the_cursor() -> None:
    bus = EventBus(SimClock())
    events = [bus.publish("timeout", model=f"m{i}") for i in range(5)]

    assert bus.since(0).events == tuple(events)
    assert bus.since(3).events == tuple(events[3:])
    assert bus.since(5).events == ()
    assert all(bus.since(seq).missed == 0 for seq in range(6))


def test_the_log_keeps_only_the_newest_events_and_says_how_many_a_reader_missed() -> None:
    bus = EventBus(SimClock(), max_events=3)
    for i in range(10):
        bus.publish("queue_full", model=f"m{i}")

    assert (bus.capacity, bus.retained, bus.last_seq) == (3, 3, 10)
    assert [e.seq for e in bus.recent(100)] == [8, 9, 10]
    # A reader that saw event 2 last has lost 3 to 7.
    page = bus.since(2)
    assert ([e.seq for e in page.events], page.missed) == ([8, 9, 10], 5)
    # One that saw event 8 has lost nothing.
    page = bus.since(8)
    assert ([e.seq for e in page.events], page.missed) == ([9, 10], 0)
    # The counters still cover everything ever published.
    assert bus.counts["queue_full"] == 10


def test_a_cursor_from_another_process_gets_everything_retained() -> None:
    bus = EventBus(SimClock())
    bus.publish("timeout", model="m")

    page = bus.since(500)

    assert ([e.seq for e in page.events], page.missed) == ([1], 0)


def test_memory_stays_bounded_after_many_events() -> None:
    bus = EventBus(SimClock(), max_events=100)
    most_retained = 0
    for i in range(100_000):
        bus.publish(EVENT_TYPES[i % len(EVENT_TYPES)], model="m", n=i)
        most_retained = max(most_retained, bus.retained)

    assert most_retained == 100
    assert bus.last_seq == 100_000
    assert bus.since(0).missed == 99_900
    assert sum(bus.counts.values()) == 100_000


def test_listeners_get_each_event_until_they_unsubscribe() -> None:
    bus = EventBus(SimClock())
    seen: list[int] = []
    also_seen: list[str] = []
    stop = bus.subscribe(lambda event: seen.append(event.seq))
    bus.subscribe(lambda event: also_seen.append(event.type))

    bus.publish("timeout", model="m")
    stop()
    stop()  # a second call does nothing
    bus.publish("downgrade", model="m", to_model="n")

    assert seen == [1]
    assert also_seen == ["timeout", "downgrade"]


def test_bus_rejects_a_log_that_would_keep_nothing() -> None:
    with pytest.raises(ValueError, match="max_events must be at least 1"):
        EventBus(SimClock(), max_events=0)
