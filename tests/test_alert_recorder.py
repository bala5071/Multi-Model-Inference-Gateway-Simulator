"""The alert recorder, its rollups and the signals (docs/43): the histogram's edges and
snapping; a recorder that never awaits and whose memory is bounded; two instances' rollups
adding up exactly to one instance's; the flusher keeping, sending once, dropping and marking
incomplete; every signal from scripted rollups with its minimum at one below and at it; what
the gateway records for generate and chat requests; that one application's signals never see
another's; and that no rollup holds a prompt, a key or an application id (Valkey).

Tests marked ``valkey`` run against the Valkey of ``docker-compose.valkey.yml``. Test values,
not secrets."""

from __future__ import annotations

import ast
import inspect
import math
import os
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway import alertrecorder
from gateway.alertrecorder import (
    EDGES,
    OVERFLOW,
    AlertRecorder,
    MinuteKey,
    bucket,
    percentile,
    snap,
)
from gateway.alertrollups import (
    AlertRollups,
    InMemoryAlertRollups,
    MinuteRollup,
    RollupFlusher,
)
from gateway.app import AlertOutcomeMiddleware, create_app
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.signals import (
    INCOMPLETE,
    RATE_MINIMUM,
    SIGNALS,
    TOO_FEW,
    SignalValue,
    budget_used,
    circuit_open,
    evaluate,
    percentile_minimum,
)
from gateway.state import StateUnavailableError
from helpers import application_access, bearer
from test_chat_routing import (
    BAD_TOOL,
    GOOD_TOOL,
    PRIMARY,
    TEXT,
    A,
    B,
    _source,
    refused,
    routing,
)
from valkey_helpers import VALKEY_URL_ENV, ManualEpoch, counters_on

REPO = Path(__file__).resolve().parent.parent
MINUTE = 60_000.0


class Epoch:
    """Calendar time the test moves."""

    def __init__(self, at_ms: float = 600 * MINUTE) -> None:
        self.at_ms = at_ms

    def epoch_ms(self) -> float:
        return self.at_ms


# --- the histogram ------------------------------------------------------------------------------


def test_the_histogram_edges_are_fixed_powers_of_two_quarters() -> None:
    assert len(EDGES) == 72
    assert EDGES[0] == 1.0
    assert EDGES[4] == 2.0
    assert EDGES[40] == 1024.0
    assert all(EDGES[k] == 2 ** (k / 4) for k in range(72))
    assert 220_000 < EDGES[-1] < 230_000  # about 220 s
    assert all(1.18 < EDGES[k + 1] / EDGES[k] < 1.19 for k in range(71))  # about 19% apart
    assert bucket(0.0) == bucket(0.5) == bucket(1.0) == 0
    assert bucket(1.0001) == 1
    assert bucket(2.0) == 4
    assert bucket(EDGES[-1]) == OVERFLOW - 1
    assert bucket(EDGES[-1] + 1) == OVERFLOW


def test_a_threshold_snaps_to_the_nearest_edge() -> None:
    assert snap(1024.0) == 1024.0
    assert snap(1000.0) == 1024.0  # 861 is farther
    assert snap(900.0) == EDGES[39]  # 861 is nearer than 1024
    for value in (0.1, 1.5, 3.0, 99.0, 250.0, 1000.0, 5000.0, 1e9):
        snapped = snap(value)
        assert snapped in EDGES
        assert all(abs(snapped - value) <= abs(edge - value) for edge in EDGES), value


def test_a_percentile_is_the_upper_edge_of_its_nearest_rank_bucket() -> None:
    histogram = {bucket(10.0): 90, bucket(100.0): 9, bucket(1000.0): 1}
    assert percentile(histogram, 0.50) == EDGES[bucket(10.0)]
    assert percentile(histogram, 0.95) == EDGES[bucket(100.0)]
    assert percentile(histogram, 0.99) == EDGES[bucket(100.0)]
    assert percentile(histogram, 1.00) == EDGES[bucket(1000.0)]
    assert percentile({OVERFLOW: 3}, 0.5) == math.inf
    with pytest.raises(ValueError, match="empty"):
        percentile({}, 0.5)


# --- the recorder -------------------------------------------------------------------------------


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.Await)]


def test_the_recorder_never_awaits_and_does_no_io() -> None:
    """Structural: the recorder's module has no coroutine and no await, imports nothing that
    does I/O, and the middleware's call into it is a plain function."""
    source = inspect.getsource(alertrecorder)
    tree = ast.parse(source)
    assert _functions(tree) == []
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert imported <= {"__future__", "bisect", "math", "collections", "typing", "gateway"}
    record = ast.parse(inspect.getsource(AlertOutcomeMiddleware))
    [method] = [
        n for n in ast.walk(record) if isinstance(n, ast.FunctionDef) and n.name == "_record"
    ]
    assert _functions(method) == []


def test_the_recorder_counts_each_outcome_under_its_fixed_name() -> None:
    epoch = Epoch()
    recorder = AlertRecorder(epoch)
    recorder.record("app1", kind="generate", status=200, ttft_ms=12.0, total_ms=40.0)
    recorder.record("app1", kind="generate", status=429, code="rate_limited")
    recorder.record("app1", kind="generate", status=200, total_ms=5.0, downgraded=True)
    recorder.record(
        "app1",
        kind="chat",
        status=200,
        fallbacks=("connect_error",),
        hosted_attempts=2,
        hosted=True,
        ttft_ms=3.0,
        total_ms=9.0,
    )
    recorder.record("app1", kind="chat", status=503, code="state_unavailable")
    recorder.record("app1", kind="chat", status=504, code="request_timeout")
    recorder.record("app1", kind="chat", status=400, code="something a client made up")
    minute = int(epoch.at_ms // MINUTE)
    assert recorder.take() == {
        ("app1", minute): {
            "requests": 7,
            "kind:generate": 3,
            "kind:chat": 4,
            "status:2xx": 3,
            "status:4xx": 2,
            "status:5xx": 2,
            "status:504": 1,
            "code:rate_limited": 1,
            "reject:rate_limited": 1,
            "code:state_unavailable": 1,
            "code:request_timeout": 1,
            "code:other": 1,
            "downgraded": 1,
            "fallback:any": 1,
            "fallback:connect_error": 1,
            "hosted_attempts": 2,
            "hosted_served": 1,
            f"ttft:{bucket(12.0)}": 1,
            f"ttft:{bucket(3.0)}": 1,
            f"total:{bucket(40.0)}": 1,
            f"total:{bucket(5.0)}": 1,
            f"total:{bucket(9.0)}": 1,
        }
    }
    assert recorder.take() == {}
    with pytest.raises(ValueError, match="kind"):
        recorder.record("app1", kind="embeddings", status=200)


def test_the_recorders_memory_is_the_applications_since_the_last_take() -> None:
    epoch = Epoch()
    recorder = AlertRecorder(epoch)
    for minute in range(3):
        epoch.at_ms = (600 + minute) * MINUTE
        for app in range(50):
            recorder.record(f"app{app}", kind="generate", status=200)
        taken = recorder.take()
        assert len(taken) == 50
        assert recorder.pending() == 0
    # Writing straight into the in-memory rollups, it keeps nothing at all.
    rollups = InMemoryAlertRollups(keep_minutes=120)
    straight = AlertRecorder(epoch, sink=rollups)
    for minute in range(300):
        epoch.at_ms = (600 + minute) * MINUTE
        straight.record("app0", kind="generate", status=200)
        straight.record("app1", kind="chat", status=200)
        assert straight.pending() == 0
    # And the rollups keep the last 120 minutes of each application.
    assert rollups.size() == 2 * 120


# --- two instances, one rollup ------------------------------------------------------------------


def _requests(seed: int, count: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    made = []
    for _ in range(count):
        status = rng.choice([200, 200, 200, 429, 502, 503, 504])
        made.append(
            {
                "application_id": rng.choice(["app1", "app2", "app3"]),
                "kind": rng.choice(["generate", "chat"]),
                "status": status,
                "code": None if status == 200 else rng.choice(["rate_limited", "queue_full"]),
                "ttft_ms": rng.uniform(1, 5000) if status == 200 else None,
                "total_ms": rng.uniform(1, 90000) if status == 200 else None,
                "hosted_attempts": rng.randint(0, 3),
                "minute_ms": rng.randint(0, 4) * MINUTE + rng.uniform(0, MINUTE - 1),
            }
        )
    return made


async def _replay(
    requests: Sequence[dict[str, Any]], recorders: int, rollups: AlertRollups
) -> None:
    """The same requests, dealt round-robin to ``recorders`` instances, each flushing into
    ``rollups`` every few requests."""
    epoch = Epoch()
    clock = SimClock()
    instances = [AlertRecorder(epoch) for _ in range(recorders)]
    flushers = [RollupFlusher(r, rollups, clock, f"instance-{i}") for i, r in enumerate(instances)]
    for n, request in enumerate(sorted(requests, key=lambda r: r["minute_ms"])):
        epoch.at_ms = 600 * MINUTE + request["minute_ms"]
        fields = {k: v for k, v in request.items() if k not in {"application_id", "minute_ms"}}
        instances[n % recorders].record(request["application_id"], **fields)
        if n % 7 == 0:
            assert await flushers[n % recorders].flush()
    for flusher in flushers:
        assert await flusher.flush()


async def _all(rollups: AlertRollups) -> dict[str, list[MinuteRollup]]:
    return {app: await rollups.read(app, range(600, 606)) for app in ("app1", "app2", "app3")}


async def test_two_instances_rollups_add_up_exactly_to_one_instances() -> None:
    requests = _requests(seed=4, count=500)
    one, two, three = InMemoryAlertRollups(), InMemoryAlertRollups(), InMemoryAlertRollups()
    await _replay(requests, 1, one)
    await _replay(requests, 2, two)
    await _replay(requests, 3, three)
    assert await _all(one) == await _all(two) == await _all(three)
    assert sum(r.counts.get("requests", 0) for rs in (await _all(two)).values() for r in rs) == 500


# --- the flusher ------------------------------------------------------------------------------


class FlakyRollups(InMemoryAlertRollups):
    """In-memory rollups that refuse while ``down``, and, with ``applies_then_fails``, take a
    batch and then fail the call, as a call that timed out after Valkey applied it."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.applies_then_fails = False

    async def add(self, batch: str, deltas: Mapping[MinuteKey, Mapping[str, int]]) -> None:
        if self.down:
            raise StateUnavailableError("timeout")
        await super().add(batch, deltas)
        if self.applies_then_fails:
            raise StateUnavailableError("timeout")

    async def mark_incomplete(self, keys: Sequence[MinuteKey]) -> None:
        if self.down:
            raise StateUnavailableError("timeout")
        await super().mark_incomplete(keys)


async def test_unsent_deltas_are_kept_sent_once_then_dropped_and_marked_incomplete() -> None:
    clock, epoch = SimClock(), Epoch()
    recorder = AlertRecorder(epoch)
    rollups = FlakyRollups()
    dropped: list[int] = []
    flusher = RollupFlusher(recorder, rollups, clock, "i1", on_dropped=dropped.append)
    minute = int(epoch.at_ms // MINUTE)
    # Down for one flush: kept, and sent with the next.
    rollups.down = True
    recorder.record("app1", kind="generate", status=200)
    assert not await flusher.flush()
    assert flusher.unsent() == 1
    rollups.down = False
    assert await flusher.flush()
    assert (await rollups.read("app1", [minute]))[0].counts["requests"] == 1
    # Applied, then the call failed: sent again, counted once.
    recorder.record("app1", kind="generate", status=200)
    rollups.applies_then_fails = True
    assert not await flusher.flush()
    rollups.applies_then_fails = False
    assert await flusher.flush()
    assert (await rollups.read("app1", [minute]))[0].counts["requests"] == 2
    # Down for ten minutes: dropped, counted, and the minute marked once Valkey answers.
    epoch.at_ms += MINUTE
    recorder.record("app2", kind="chat", status=200)
    rollups.down = True
    assert not await flusher.flush()
    await clock.sleep_ms(9 * MINUTE)
    assert not await flusher.flush()
    assert dropped == []  # kept for ten minutes
    await clock.sleep_ms(MINUTE)
    assert not await flusher.flush()
    assert (dropped, flusher.dropped, flusher.unsent()) == ([1], 1, 0)
    rollups.down = False
    recorder.record("app2", kind="chat", status=200)  # a later request, same minute
    assert await flusher.flush()
    [lost] = await rollups.read("app2", [minute + 1])
    assert lost.incomplete
    assert lost.counts == {"requests": 1, "kind:chat": 1, "status:2xx": 1}
    [kept] = await rollups.read("app1", [minute])
    assert not kept.incomplete


async def test_the_flusher_runs_every_ten_seconds_on_the_injected_clock() -> None:
    clock, epoch = SimClock(), Epoch()
    recorder = AlertRecorder(epoch)
    rollups = InMemoryAlertRollups()
    flusher = RollupFlusher(recorder, rollups, clock, "i1")
    recorder.record("app1", kind="generate", status=200)
    import asyncio

    task = asyncio.ensure_future(flusher.run())
    await clock.sleep_ms(9_999.0)
    assert recorder.pending() == 1
    await clock.sleep_ms(2.0)
    assert recorder.pending() == 0
    task.cancel()
    minute = int(epoch.at_ms // MINUTE)
    assert (await rollups.read("app1", [minute]))[0].counts["requests"] == 1


# --- the signals --------------------------------------------------------------------------------


def _minutes(counts: Sequence[Mapping[str, int]], *, app: str = "app1") -> list[MinuteRollup]:
    return [MinuteRollup(app, 600 + i, dict(c)) for i, c in enumerate(counts)]


def _window(total: Mapping[str, int], window: int) -> list[MinuteRollup]:
    """``total`` in the window's first minute, the others empty."""
    return _minutes([total] + [{}] * (window - 1))


def test_the_minimums_are_the_plans() -> None:
    assert [percentile_minimum(p) for p in (0.50, 0.95, 0.99)] == [10, 100, 500]
    assert RATE_MINIMUM == 20
    assert set(SIGNALS) == {
        *(f"latency_{k}_{p}" for k in ("total", "ttft") for p in ("p50", "p95", "p99")),
        "error_rate", "timeout_rate", "state_unavailable_rate", "rejections",
        "rejection_rate", "fallback_rate", "hosted_share", "downgrade_rate", "requests",
        "budget_used", "circuit_open", "key_expires_in", "reservation_expired",
    }  # fmt: skip
    assert SIGNALS["state_unavailable_rate"].windows == (5, 15)
    assert SIGNALS["fallback_rate"].windows == (15, 60)


@pytest.mark.parametrize(("p", "minimum"), [("p50", 10), ("p95", 100), ("p99", 500)])
@pytest.mark.parametrize("kind", ["total", "ttft"])
def test_a_latency_percentile_is_too_few_one_below_its_minimum(
    kind: str, p: str, minimum: int
) -> None:
    def at(n: int) -> SignalValue:
        # n requests: all at 10 ms but the slowest five at 1000 ms.
        counts = {"requests": n, f"{kind}:{bucket(10.0)}": n - 5, f"{kind}:{bucket(1000.0)}": 5}
        return evaluate(f"latency_{kind}_{p}", "app1", _window(counts, 5), window=5)

    assert at(minimum - 1) == TOO_FEW
    # At the minimum, five requests are above the percentile: it is the fast ones' edge.
    assert at(minimum) == SignalValue(EDGES[bucket(10.0)])
    # One more slow request than that puts the percentile on the slow ones' edge for p99.
    if p == "p99":
        counts = {"requests": 500, f"{kind}:{bucket(10.0)}": 494, f"{kind}:{bucket(1000.0)}": 6}
        slow = evaluate(f"latency_{kind}_{p}", "app1", _window(counts, 5), window=5)
        assert slow == SignalValue(EDGES[bucket(1000.0)])


@pytest.mark.parametrize(
    ("name", "parameter", "numerator", "denominator", "window"),
    [
        ("error_rate", None, "status:5xx", "requests", 5),
        ("timeout_rate", None, "status:504", "requests", 15),
        ("state_unavailable_rate", None, "code:state_unavailable", "requests", 5),
        ("rejection_rate", "budget_exhausted", "reject:budget_exhausted", "requests", 60),
        ("fallback_rate", "any", "fallback:any", "kind:chat", 15),
        ("fallback_rate", "attempt_cap", "fallback:attempt_cap", "kind:chat", 60),
        ("hosted_share", None, "hosted_served", "kind:chat", 15),
        ("downgrade_rate", None, "downgraded", "kind:generate", 60),
    ],
)
def test_a_rate_is_too_few_below_twenty_and_a_percent_at_twenty(
    name: str, parameter: str | None, numerator: str, denominator: str, window: int
) -> None:
    def at(n: int) -> SignalValue:
        counts = {"requests": n, denominator: n, numerator: 5}
        return evaluate(name, "app1", _window(counts, window), window=window, parameter=parameter)

    assert at(RATE_MINIMUM - 1) == TOO_FEW
    assert at(RATE_MINIMUM) == SignalValue(25.0)
    assert at(40) == SignalValue(12.5)


def test_counts_are_never_too_few_but_an_incomplete_minute_is_not_judged() -> None:
    quiet = evaluate("requests", "app1", _window({}, 5), window=5)
    assert quiet == SignalValue(0.0)
    rejected = evaluate(
        "rejections",
        "app1",
        _window({"reject:queue_full": 3}, 15),
        window=15,
        parameter="queue_full",
    )
    assert rejected == SignalValue(3.0)
    lost = _window({"requests": 1000, "status:5xx": 900}, 5)
    lost[3] = MinuteRollup("app1", lost[3].minute, {}, incomplete=True)
    for name in ("requests", "error_rate", "latency_total_p50"):
        assert evaluate(name, "app1", lost, window=5) == INCOMPLETE


def test_a_signal_takes_one_applications_window_and_nothing_else() -> None:
    five = _window({"requests": 30}, 5)
    with pytest.raises(ValueError, match="one application"):
        evaluate("requests", "app2", five, window=5)
    with pytest.raises(ValueError, match="consecutive"):
        evaluate("requests", "app1", five[:4], window=5)
    with pytest.raises(ValueError, match="window"):
        evaluate("requests", "app1", _window({}, 10), window=10)
    with pytest.raises(ValueError, match="window"):
        evaluate("state_unavailable_rate", "app1", _window({}, 60), window=60)
    with pytest.raises(ValueError, match="parameter"):
        evaluate("rejections", "app1", five, window=5)
    with pytest.raises(ValueError, match="parameter"):
        evaluate("rejections", "app1", five, window=5, parameter="cosmic_rays")
    with pytest.raises(ValueError, match="rollups"):
        evaluate("budget_used", "app1", five, window=5)


def test_the_durable_signals() -> None:
    assert budget_used(800, 1000) == SignalValue(80.0)
    assert budget_used(5, 0) == SignalValue(None, "no_budget")
    assert circuit_open(None) == SignalValue(None, "unavailable")
    assert circuit_open(3.0) == SignalValue(3.0)


# --- what the gateway records -------------------------------------------------------------------


@asynccontextmanager
async def gateway(**limits: tuple[int | None, int | None]) -> AsyncIterator[Any]:
    clock = SimClock()
    access, keys = application_access(
        clock, apps={"alpha": "pro", "beta": "pro"}, limits=limits or None
    )
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock, applications=access)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield client, keys, app, clock
    await registry.aclose()


async def _rollup(app: Any, application_id: str, clock: SimClock) -> dict[str, int]:
    minute = int(ClockEpoch(clock).epoch_ms() // MINUTE)
    found = await app.state.alert_rollups.read(application_id, range(0, minute + 1))
    total: dict[str, int] = {}
    for rollup in found:
        for name, n in rollup.counts.items():
            total[name] = total.get(name, 0) + n
    return total


async def test_generate_requests_are_recorded_with_their_outcome_and_latency() -> None:
    async with gateway(beta=(None, 1)) as (client, keys, app, clock):
        ok = await client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": "Hi there."},
            headers=bearer(keys["alpha"]),
        )
        for _ in range(2):  # the second over beta's one request a minute
            await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": "Hi there."},
                headers=bearer(keys["beta"]),
            )
        await client.post(
            "/v1/generate", json={"model": "economy", "prompt": "x"}, headers=bearer("gwk_nope")
        )
        await client.post("/v1/generate", json={"model": "nope", "prompt": "x"},
                          headers=bearer(keys["alpha"]))  # fmt: skip
        alpha = await _rollup(app, "alpha", clock)
        beta = await _rollup(app, "beta", clock)
    timing = ok.json()["timing"]
    assert alpha == {
        "requests": 2,
        "kind:generate": 2,
        "status:2xx": 1,
        "status:4xx": 1,
        "code:other": 1,
        f"ttft:{bucket(timing['ttft_ms'])}": 1,
        f"total:{bucket(timing['total_ms'])}": 1,
    }
    assert beta["requests"] == 2
    assert (beta["reject:rate_limited"], beta["code:rate_limited"]) == (1, 1)
    # A request with no valid key belongs to no application: it is not recorded.
    assert sum(1 for _ in app.state.alert_rollups._minutes) == 2


async def test_one_applications_rollups_and_signals_never_see_anothers() -> None:
    """alpha and beta on the same model: beta's refusals and its slow minute are in no number
    of alpha's, and each application's rollup counts its own requests only."""
    async with gateway(beta=(None, 1)) as (client, keys, app, clock):
        for _ in range(25):
            await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": "Hi there."},
                headers=bearer(keys["alpha"]),
            )
        for _ in range(30):
            await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": "Hi there."},
                headers=bearer(keys["beta"]),
            )
        minute = int(ClockEpoch(clock).epoch_ms() // MINUTE)
        alpha = await app.state.alert_rollups.read("alpha", range(minute - 4, minute + 1))
        beta = await app.state.alert_rollups.read("beta", range(minute - 4, minute + 1))
    assert sum(r.counts.get("requests", 0) for r in alpha) == 25
    assert sum(r.counts.get("requests", 0) for r in beta) == 30
    assert evaluate("rejection_rate", "alpha", alpha, window=5, parameter="rate_limited") == (
        SignalValue(0.0)
    )
    beta_rate = evaluate("rejection_rate", "beta", beta, window=5, parameter="rate_limited")
    assert beta_rate.value is not None
    assert beta_rate.value > 90


async def test_chat_requests_are_recorded_with_their_route() -> None:
    """A hosted primary refused, the next answering (a fallback, two hosted attempts, hosted);
    a retry of invalid tool JSON (two attempts, no fallback); the gateway's tier answering."""
    async with routing() as r:
        await r.mode(PRIMARY)
        await r.provider(A, "a.provider.test", role="primary", tools=True)
        await r.provider(B, "b.provider.test", role="primary", tools=True)
        r.org_net.responses = [refused(), TEXT, BAD_TOOL, GOOD_TOOL]
        r.op_net.responses = [TEXT]
        fell_back = await r.chat()
        retried = await r.chat(tools=True)
        minute = int(r.world.accounts.epoch.epoch_ms() // MINUTE)
        [rollup] = await r.app.state.alert_rollups.read("north-shop", [minute])
    assert _source(fell_back) == (B, "hosted", A, "connect_error")
    assert _source(retried) == (A, "hosted", None, None)
    counts = rollup.counts
    assert counts["requests"] == counts["kind:chat"] == counts["status:2xx"] == 2
    assert (counts["fallback:any"], counts["fallback:connect_error"]) == (1, 1)
    assert counts["hosted_attempts"] == 4
    assert counts["hosted_served"] == 2


async def test_a_streaming_chat_answer_is_recorded_when_it_ends() -> None:
    async with routing() as r:
        r.op_net.responses = [TEXT]
        streamed = await r.chat(stream=True)
        minute = int(r.world.accounts.epoch.epoch_ms() // MINUTE)
        [rollup] = await r.app.state.alert_rollups.read("north-shop", [minute])
    assert streamed.status_code == 200
    assert rollup.counts["requests"] == 1
    assert sum(n for k, n in rollup.counts.items() if k.startswith("total:")) == 1


async def test_without_applications_there_is_no_recorder() -> None:
    """The perf gate's server: no applications, so nothing recorded and no middleware."""
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock)
    assert app.state.alert_recorder is None
    classes: list[object] = [m.cls for m in app.user_middleware]
    assert AlertOutcomeMiddleware not in classes
    await registry.aclose()


# --- Valkey -------------------------------------------------------------------------------------


@asynccontextmanager
async def valkey_rollups(count: int = 1) -> AsyncIterator[list[Any]]:
    url = os.environ[VALKEY_URL_ENV]
    prefix = f"gwtest:{random.SystemRandom().getrandbits(64):016x}:"
    found = [counters_on(url, ManualEpoch(), prefix=prefix, seed=i + 1) for i in range(count)]
    try:
        yield found
    finally:
        client = found[0].client
        async for key in client.scan_iter(match=prefix + "*"):
            await client.delete(key)
        for counters in found:
            await counters.aclose()


@pytest.mark.valkey
async def test_valkey_rollups_of_two_instances_equal_one_instances_in_memory() -> None:
    requests = _requests(seed=9, count=300)
    memory = InMemoryAlertRollups()
    await _replay(requests, 1, memory)
    async with valkey_rollups(2) as (first, second):
        shared = first.alert_rollups()
        await _replay(requests, 2, shared)
        from_first = await _all(shared)
        from_second = await _all(second.alert_rollups())
    assert from_first == from_second == await _all(memory)


@pytest.mark.valkey
async def test_valkey_rollups_take_a_batch_once_mark_minutes_and_a_restart() -> None:
    async with valkey_rollups() as [counters]:
        rollups = counters.alert_rollups()
        deltas = {("app1", 600): {"requests": 2}, ("app1", 601): {"requests": 1}}
        await rollups.add("i1:1", deltas)
        await rollups.add("i1:1", deltas)  # sent again: nothing added
        await rollups.add("i1:2", {("app1", 601): {"requests": 4}})
        await rollups.mark_incomplete([("app1", 601)])
        before = await rollups.read("app1", [600, 601, 602])
        await rollups.note_restart(600)
        after = await rollups.read("app1", [600, 601, 602])
        ttl = await counters.client.pttl(counters.rollup_name("rollup", "app1\x00600"))
    assert [(r.counts, r.incomplete) for r in before] == [
        ({"requests": 2}, False),
        ({"requests": 5}, True),
        ({}, False),
    ]
    assert [r.incomplete for r in after] == [True, True, False]
    assert 0 < ttl <= 2 * 60 * 60 * 1000


@pytest.mark.valkey
async def test_valkey_rollups_refuse_when_valkey_cannot_be_reached() -> None:
    counters = counters_on("redis://127.0.0.1:1/0", ManualEpoch(), prefix="gwtest:nowhere:")
    rollups = counters.alert_rollups()
    with pytest.raises(StateUnavailableError):
        await rollups.add("i1:1", {("app1", 600): {"requests": 1}})
    with pytest.raises(StateUnavailableError):
        await rollups.read("app1", [600])
    await counters.aclose()


@pytest.mark.valkey
async def _scan(client: Any, match: str) -> str:
    """Every key under ``match``, and every hash field, value and string in it."""
    stored: list[str] = []
    async for key in client.scan_iter(match=match):
        stored.append(key)
        kind = await client.type(key)
        if kind == "hash":
            stored += [f"{k}={v}" for k, v in (await client.hgetall(key)).items()]
        elif kind == "string":
            stored.append(str(await client.get(key)))
    return "\n".join(stored)


async def test_no_rollup_key_or_value_holds_an_id_a_prompt_or_a_key() -> None:
    """A request with a planted prompt and key is recorded and flushed into Valkey; every key,
    field and value under the test's prefix is scanned for them and for the application id.
    The control: the same scan over a deliberately unredacted copy (a hash named by the
    application id, holding the prompt and the key, under a prefix of its own) finds each."""
    planted_prompt = "planted prompt: the orchid code is 7731"
    async with gateway() as (client, keys, _, clock), valkey_rollups() as [counters]:
        planted = {"prompt": planted_prompt, "api key": keys["alpha"], "application": "alpha"}
        recorder = AlertRecorder(ClockEpoch(clock))
        flusher = RollupFlusher(recorder, counters.alert_rollups(), clock, "i1")
        response = await client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": planted_prompt},
            headers=bearer(keys["alpha"]),
        )
        recorder.record("alpha", kind="generate", status=response.status_code,
                        total_ms=response.json()["timing"]["total_ms"])  # fmt: skip
        assert await flusher.flush()
        text = await _scan(counters.client, counters.prefix + "*")
        control_prefix = counters.prefix + "control:"
        await counters.client.hset(
            f"{control_prefix}alpha", mapping={"prompt": planted_prompt, "key": keys["alpha"]}
        )
        control = await _scan(counters.client, control_prefix + "*")
        text = text.replace(control, "")  # the control is under the test's prefix too
    assert "requests=1" in text  # the rollup is there
    for name, value in planted.items():
        assert value in control, name  # the scan finds what is there
        assert value not in text, name
