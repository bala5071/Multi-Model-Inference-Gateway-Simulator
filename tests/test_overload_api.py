"""Overload protection through the HTTP API, on simulated time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from benchmarks import overload as overload_benchmark

from gateway.app import DEFAULT_OVERLOAD, create_app, create_default_app
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.metrics import percentile
from gateway.overload import OverloadPolicy, SloMonitor, SloPolicy
from gateway.profiles import Tier
from gateway.routing import Plan, PlansConfig
from helpers import TEST_PROFILES, application_access, application_headers, bearer

PLANS = PlansConfig(
    plans={
        "enterprise": Plan("enterprise", ("economy", "standard", "premium"), 1.0, 10**9),
        "pro": Plan("pro", ("economy", "standard"), 1.0, 10**9),
        "premium_only": Plan("premium_only", ("premium",), 1.0, 10**9),
    }
)
APPS = {"acme": "enterprise", "globex": "pro", "solo": "premium_only"}
# Premium's SLO is 2 s; it counts as recovered under 1 s.
SLO = SloPolicy(
    p99_ms={"premium": 2000.0, "standard": 2000.0},
    window_ms=10_000.0,
    min_samples=5,
    recover_ratio=0.5,
    min_hold_ms=1000.0,
)


@dataclass
class Gateway:
    client: httpx.AsyncClient
    clock: SimClock
    monitor: SloMonitor | None
    keys: dict[str, str]
    counter: int = 0

    async def generate(self, **body: Any) -> httpx.Response:
        # A fresh ten-word prompt each time, so nothing is served from the prefix cache.
        self.counter += 1
        body.setdefault("prompt", " ".join(f"p{self.counter}w{i}" for i in range(10)))
        body.setdefault("max_tokens", 2)
        headers = application_headers(body, self.keys)
        return await self.client.post("/v1/generate", json=body, headers=headers)

    async def usage(self, application: str) -> dict[str, Any]:
        response = await self.client.get(
            "/v1/applications/me/usage", headers=bearer(self.keys[application])
        )
        data: dict[str, Any] = response.json()
        return data

    async def status(self) -> dict[str, Any]:
        data: dict[str, Any] = (await self.client.get("/v1/overload")).json()
        return data


@asynccontextmanager
async def _gateway(
    *,
    plans: PlansConfig | None = None,
    overload: OverloadPolicy | None = None,
    max_queue_len: Mapping[Tier, int] | None = None,
) -> AsyncIterator[Gateway]:
    clock = SimClock()
    registry = build_continuous_registry(
        clock, seed=7, max_queue_len=max_queue_len, profiles=TEST_PROFILES
    )
    access, keys = (None, {}) if plans is None else application_access(clock, plans, apps=APPS)
    app = create_app(registry, clock, applications=access, overload=overload)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, app.state.slo_monitor, keys)
    await registry.aclose()


async def _overload_premium(gateway: Gateway, **body: Any) -> list[httpx.Response]:
    """Eight premium requests at once, of up to 60 tokens each.

    Output lengths come from the seeded backend, capped at 60, so the requests finish at
    different times. The slowest take well over the 2 s SLO.
    """
    return list(
        await asyncio.gather(
            *(gateway.generate(model="premium", max_tokens=60, **body) for _ in range(8))
        )
    )


# --- admission control ---------------------------------------------------------------------


async def test_requests_beyond_the_queue_limit_get_429_with_retry_after() -> None:
    async with _gateway(plans=PLANS, max_queue_len={Tier.PREMIUM: 2}) as gateway:
        # Premium seats 8 at a time and may queue 2 more: 10 of these 12 are accepted.
        responses = await asyncio.gather(
            *(gateway.generate(application_id="acme", model="premium") for _ in range(12)),
            gateway.generate(application_id="acme", model="economy"),
        )
        usage = await gateway.usage("acme")
        metrics = (await gateway.client.get("/metrics/summary")).json()["models"]

    *premium, economy = responses
    assert [r.status_code for r in premium] == [200] * 10 + [429] * 2
    rejected = premium[-1]
    assert rejected.headers["Retry-After"] == "1"
    assert "X-Model-Used" not in rejected.headers
    assert rejected.json() == {
        "error": {
            "code": "queue_full",
            "message": "Model 'premium' is overloaded: 2 requests are already queued or "
            "in progress (limit 2). Retry in 1 seconds.",
            "model": "premium",
            "waiting": 2,
            "limit": 2,
            "retry_after_s": 1,
        }
    }
    # Another model is not affected, and rejected requests are neither charged nor counted.
    assert economy.status_code == 200
    assert usage["by_model"]["premium"]["requests"] == 10
    assert metrics["premium"]["requests"] == 10


async def test_queue_has_room_again_once_requests_finish() -> None:
    async with _gateway(max_queue_len={Tier.PREMIUM: 0}) as gateway:
        first = await asyncio.gather(*(gateway.generate(model="premium") for _ in range(9)))
        second = await gateway.generate(model="premium")
        status = await gateway.status()

    assert [r.status_code for r in first] == [200] * 8 + [429]
    assert second.status_code == 200
    assert status["models"]["premium"]["waiting"] == 0


# --- timeouts ------------------------------------------------------------------------------


async def test_request_over_the_timeout_gets_504_and_frees_its_capacity() -> None:
    policy = OverloadPolicy(request_timeout_ms=200.0)
    async with _gateway(plans=PLANS, overload=policy) as gateway:
        # At least 16 decode steps of 30 ms each: far over the 200 ms timeout.
        slow = await gateway.generate(application_id="acme", model="premium", max_tokens=50)
        timed_out_at = gateway.clock.now_ms()
        quick = await gateway.generate(application_id="acme", model="premium", max_tokens=2)
        usage = await gateway.usage("acme")
        metrics = (await gateway.client.get("/metrics/summary")).json()["models"]
        status = await gateway.status()

    assert slow.status_code == 504
    assert timed_out_at == 200.0
    assert slow.json() == {
        "error": {
            "code": "request_timeout",
            "message": "Model 'premium' did not finish the request within 200 ms. "
            "It was cancelled and has not been charged.",
            "model": "premium",
            "timeout_ms": 200.0,
        }
    }
    # The cancelled request left the batch at the next iteration boundary (its steps end
    # at 186, 216, ...), so the next one waited 16 ms and not for the rest of its decode.
    assert quick.status_code == 200
    assert quick.json()["timing"] == {"queue_ms": 16.0, "ttft_ms": 52.0, "total_ms": 82.0}
    # Only the request that finished is charged and counted.
    assert usage["total"]["requests"] == 1
    assert metrics["premium"]["requests"] == 1
    assert status["request_timeout_ms"] == 200.0
    assert status["models"]["premium"]["waiting"] == 0


async def test_request_within_the_timeout_is_unaffected() -> None:
    async with _gateway(overload=OverloadPolicy(request_timeout_ms=200.0)) as gateway:
        response = await gateway.generate(model="premium", max_tokens=2)
    assert response.status_code == 200
    assert response.json()["timing"]["total_ms"] == 66.0


async def test_timed_out_requests_count_towards_the_slo() -> None:
    policy = OverloadPolicy(
        request_timeout_ms=200.0,
        slo=SloPolicy(p99_ms={"premium": 100.0}, min_samples=2, min_hold_ms=0.0),
    )
    async with _gateway(overload=policy) as gateway:
        for _ in range(2):
            slow = await gateway.generate(model="premium", max_tokens=50)
            assert slow.status_code == 504
        status = await gateway.status()
        moved = await gateway.generate(model="premium", allow_downgrade=True)

    # Without recording timeouts the monitor would have seen no requests at all.
    premium = status["models"]["premium"]
    assert (premium["degraded"], premium["recent_p99_ms"], premium["samples"]) == (True, 200.0, 2)
    assert moved.headers["X-Downgraded-From"] == "premium"


# --- SLO-aware downgrade -------------------------------------------------------------------


async def test_downgrade_engages_under_overload_and_recovers_afterwards() -> None:
    async with _gateway(plans=PLANS, overload=OverloadPolicy(slo=SLO)) as gateway:
        assert gateway.monitor is not None

        # Phase 1: overload. The model is healthy when these arrive, so none is moved.
        surge = await _overload_premium(gateway, application_id="acme", allow_downgrade=True)
        assert [r.status_code for r in surge] == [200] * 8
        assert all("X-Downgraded-From" not in r.headers for r in surge)
        latencies = sorted(r.json()["timing"]["total_ms"] for r in surge)
        slowest_ms = latencies[-1]
        assert slowest_ms > SLO.p99_ms["premium"]
        assert gateway.clock.now_ms() == slowest_ms

        # Phase 2: premium is now degraded.
        status = await gateway.status()
        assert status["models"]["premium"] == {
            "degraded": True,
            "slo_p99_ms": 2000.0,
            "recent_p99_ms": slowest_ms,
            "samples": 8,
            "waiting": 0,
        }
        assert status["models"]["standard"]["degraded"] is False

        moved = await gateway.generate(application_id="acme", model="premium", allow_downgrade=True)
        assert moved.status_code == 200
        assert moved.headers["X-Downgraded-From"] == "premium"
        assert moved.headers["X-Model-Used"] == "standard"
        assert moved.json()["model"] == "standard"

        by_quality = await gateway.generate(
            application_id="acme", quality="best", allow_downgrade=True
        )
        assert by_quality.headers["X-Downgraded-From"] == "premium"
        assert by_quality.headers["X-Model-Used"] == "standard"

        # A request that did not opt in stays on premium, degraded or not.
        for extra in ({}, {"allow_downgrade": False}):
            stayed = await gateway.generate(application_id="acme", model="premium", **extra)
            assert stayed.headers["X-Model-Used"] == "premium"
            assert "X-Downgraded-From" not in stayed.headers

        # Those two were fast (66 ms), but the window's p99 is still the slow surge:
        # a couple of quick requests do not flip the state back.
        assert (await gateway.status())["models"]["premium"]["degraded"] is True

        # A healthy model is not downgraded, and a downgrade never leaves the plan.
        healthy = await gateway.generate(
            application_id="globex", model="standard", allow_downgrade=True
        )
        assert healthy.headers["X-Model-Used"] == "standard"
        assert "X-Downgraded-From" not in healthy.headers
        no_cheaper = await gateway.generate(
            application_id="solo", model="premium", allow_downgrade=True
        )
        assert no_cheaper.headers["X-Model-Used"] == "premium"
        assert "X-Downgraded-From" not in no_cheaper.headers

        # The downgraded requests were billed as standard requests.
        usage = await gateway.usage("acme")
        assert {m: u["requests"] for m, u in usage["by_model"].items()} == {
            "premium": 10,
            "standard": 2,
        }

        # Phase 3: the load is gone. Once the slow requests leave the 10 s window, premium
        # recovers and opted-in requests go back to it.
        await gateway.clock.sleep_ms(11_000.0)
        back = await gateway.generate(application_id="acme", model="premium", allow_downgrade=True)
        assert back.headers["X-Model-Used"] == "premium"
        assert "X-Downgraded-From" not in back.headers
        assert (await gateway.status())["models"]["premium"]["degraded"] is False

        # Exactly one change each way: no flapping in between.
        engaged, recovered = gateway.monitor.transitions
        assert (engaged.model, engaged.degraded) == ("premium", True)
        assert (recovered.model, recovered.degraded, recovered.p99_ms) == ("premium", False, None)
        # It engaged the moment a finished request first took the window's p99 over the
        # SLO. All eight arrived at t=0, so that request's latency is also the time.
        assert engaged.p99_ms is not None
        assert engaged.p99_ms > SLO.p99_ms["premium"]
        assert engaged.at_ms == engaged.p99_ms
        assert engaged.p99_ms in latencies


async def test_downgrade_skips_past_a_cheaper_model_that_is_also_degraded() -> None:
    async with _gateway(plans=PLANS, overload=OverloadPolicy(slo=SLO)) as gateway:
        await _overload_premium(gateway, application_id="acme")
        # Sixteen standard requests of up to 150 tokens fill its batch; the slowest take
        # more than its 2 s SLO as well.
        await asyncio.gather(
            *(
                gateway.generate(application_id="acme", model="standard", max_tokens=150)
                for _ in range(16)
            )
        )
        status = await gateway.status()
        moved = await gateway.generate(application_id="acme", model="premium", allow_downgrade=True)

    assert status["models"]["premium"]["degraded"] is True
    assert status["models"]["standard"]["degraded"] is True
    assert moved.headers["X-Downgraded-From"] == "premium"
    assert moved.headers["X-Model-Used"] == "economy"


async def test_downgrade_works_without_plans_over_all_registered_models() -> None:
    async with _gateway(overload=OverloadPolicy(slo=SLO)) as gateway:
        await _overload_premium(gateway)
        moved = await gateway.generate(model="llama-3.1-8b", allow_downgrade=True)
    assert moved.headers["X-Downgraded-From"] == "premium"
    assert moved.headers["X-Model-Used"] == "standard"


async def test_allow_downgrade_does_nothing_without_an_slo_policy() -> None:
    async with _gateway() as gateway:
        await _overload_premium(gateway)
        response = await gateway.generate(model="premium", allow_downgrade=True)
        status = await gateway.status()

    assert response.headers["X-Model-Used"] == "premium"
    assert "X-Downgraded-From" not in response.headers
    assert gateway.monitor is None
    assert status["request_timeout_ms"] is None
    assert status["models"]["premium"] == {
        "degraded": False,
        "slo_p99_ms": None,
        "recent_p99_ms": None,
        "samples": 0,
        "waiting": 0,
    }


async def test_allow_downgrade_must_be_a_boolean() -> None:
    async with _gateway() as gateway:
        response = await gateway.generate(model="premium", allow_downgrade="sometimes")
    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == ["allow_downgrade"]


# --- default wiring and the benchmark ------------------------------------------------------


async def test_default_app_has_overload_protection_switched_on() -> None:
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        status = (await client.get("/v1/overload")).json()

    assert status["request_timeout_ms"] == DEFAULT_OVERLOAD.request_timeout_ms
    assert DEFAULT_OVERLOAD.slo is not None
    assert {m: s["slo_p99_ms"] for m, s in status["models"].items()} == dict(
        DEFAULT_OVERLOAD.slo.p99_ms
    )
    assert all(s["degraded"] is False for s in status["models"].values())


async def test_benchmark_shows_downgrade_cutting_p99_without_flapping() -> None:
    # On the hand-made test profiles: this pins the mechanism, it is not a result.
    plain, no_transitions = await overload_benchmark.run(None, profiles=TEST_PROFILES)
    protected, transitions = await overload_benchmark.run(
        OverloadPolicy(slo=overload_benchmark.SLO), profiles=TEST_PROFILES
    )

    assert no_transitions == ()
    assert all(o.ok for o in [*plain, *protected])
    assert all(o.model_used == "premium" and o.downgraded_from is None for o in plain)

    plain_p99 = percentile([o.total_ms for o in plain], 99)
    protected_p99 = percentile([o.total_ms for o in protected], 99)
    assert protected_p99 < plain_p99 / 2

    moved = [o for o in protected if o.downgraded_from is not None]
    assert moved
    assert all(o.downgraded_from == "premium" and o.model_used == "standard" for o in moved)
    assert len(moved) + sum(1 for o in protected if o.model_used == "premium") == len(protected)
    # One overload, one recovery: the surge does not make the state flap.
    assert [(t.model, t.degraded) for t in transitions] == [("premium", True), ("premium", False)]
    # Being served by a cheaper model also costs less.
    assert sum(o.cost_usd for o in protected) < sum(o.cost_usd for o in plain)
