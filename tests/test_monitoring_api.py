"""The monitoring endpoints, on simulated time: exact values, availability, bounded memory."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.app import create_app, create_default_app
from gateway.backend_config import BackendConfig, LlamaServer, build_llamacpp_registry
from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.events import EventBus
from gateway.metrics import MetricsStore
from gateway.monitoring import Monitor, SchedulerView, render_prometheus
from gateway.overload import OverloadPolicy, SloPolicy
from gateway.passthrough import PassthroughWorker
from gateway.prefix_cache import PrefixCache
from gateway.profiles import Tier
from gateway.registry import ModelRegistry
from gateway.routing import Plan, PlansConfig, load_plans
from gateway.schema import RequestResult
from gateway.timeseries import TimeSeriesStore
from helpers import EXACT
from mock_llama import MockLlamaServer

# Ten words: 10 prompt tokens, so 5 ms of prefill on the EXACT profile. With three output
# tokens a request alone in the batch takes 5 + 3 * 4 = 17 ms and holds 13 KV tokens.
PROMPT = "one two three four five six seven eight nine ten"
COST = (10 * 0.5 + 3 * 1.0) / 1000

NO_EVENTS = {
    "state_change": 0,
    "queue_full": 0,
    "timeout": 0,
    "downgrade": 0,
    "budget_exhausted": 0,
}
UNAVAILABLE = {
    "queue": {"available": False, "length": None},
    "batch": {"available": False, "size": None, "max_size": None},
    "kv": {"available": False, "used_tokens": None, "capacity_tokens": None},
    "cache": {
        "available": False,
        "size_tokens": None,
        "lookups_total": None,
        "hits_total": None,
        "prompt_tokens_total": None,
        "cached_tokens_total": None,
        "hit_rate": None,
        "token_hit_rate": None,
    },
}
NO_SLO = {
    "available": False,
    "degraded": None,
    "slo_p99_ms": None,
    "recent_p99_ms": None,
    "samples": None,
}
# The fields of a point that only a worker with its own scheduler can fill.
SCHEDULER_FIELDS = (
    "queue_len",
    "queue_len_max",
    "batch_size",
    "batch_size_max",
    "kv_used_tokens",
    "kv_used_tokens_max",
    "cache_lookups",
    "cache_hit_rate",
    "cache_token_hit_rate",
)


def _worker(
    clock: SimClock, *, max_queue_len: int | None = None, cache: bool = True
) -> ContinuousBatchingWorker:
    """A continuous batching worker on the EXACT profile: batch of 4, 1024 KV tokens."""
    return ContinuousBatchingWorker(
        EXACT,
        SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30),
        clock,
        prefix_cache=PrefixCache(EXACT.kv_capacity_tokens) if cache else None,
        max_queue_len=max_queue_len,
    )


def _prompt(first_word: str) -> str:
    """Ten words that share no prefix with a prompt starting differently: no cache hit."""
    return f"{first_word} two three four five six seven eight nine ten"


@dataclass
class Gateway:
    client: httpx.AsyncClient
    clock: SimClock
    app: Any

    async def generate(self, **body: Any) -> httpx.Response:
        body.setdefault("max_tokens", 3)
        return await self.client.post("/v1/generate", json=body)

    async def snapshot(self, **params: Any) -> dict[str, Any]:
        response = await self.client.get("/v1/monitor/snapshot", params=params)
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        return data

    async def metrics(self) -> str:
        response = await self.client.get("/metrics")
        assert response.status_code == 200
        return response.text


@asynccontextmanager
async def _gateway(
    registry: ModelRegistry, clock: SimClock, **app_kwargs: Any
) -> AsyncIterator[Gateway]:
    app = create_app(registry, clock, **app_kwargs)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, app)
    await registry.aclose()


def _single(clock: SimClock, **worker_kwargs: Any) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register("standard", _worker(clock, **worker_kwargs))
    return registry


def _sse(response: httpx.Response) -> list[tuple[str, dict[str, Any], str | None]]:
    """Parse a server-sent-event body into (event name, decoded data, id) triples."""
    events = []
    for block in response.text.split("\n\n"):
        if not block:
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        assert set(fields) <= {"id", "event", "data"}
        events.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return events


def _point(**changes: Any) -> dict[str, Any]:
    """A point of a step in which the model was looked at and nothing else happened."""
    point: dict[str, Any] = {
        "t_ms": 0.0,
        "final": False,
        "arrived": 0,
        "completed": 0,
        "req_per_s": 0.0,
        "output_tokens": 0,
        "tokens_per_s": 0.0,
        "queue_full": 0,
        "timeouts": 0,
        "downgrades": 0,
        "in_flight": 0,
        "in_flight_max": 0,
        "queue_len": 0,
        "queue_len_max": 0,
        "batch_size": 0,
        "batch_size_max": 0,
        "kv_used_tokens": 0,
        "kv_used_tokens_max": 0,
        "cache_lookups": 0,
        "cache_hit_rate": None,
        "cache_token_hit_rate": None,
        "ttft_p50_ms": None,
        "ttft_p99_ms": None,
        "itl_p50_ms": None,
        "itl_p99_ms": None,
        "total_p50_ms": None,
        "total_p99_ms": None,
        "latency_truncated": False,
        "degraded": None,
    }
    assert set(changes) <= set(point)
    return {**point, **changes}


# --- exact values --------------------------------------------------------------------------


async def test_snapshot_of_one_request_has_exact_values() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        before = await gateway.snapshot()
        response = await gateway.generate(model="standard", prompt=PROMPT)
        after = await gateway.snapshot()

    assert response.json()["timing"] == {"queue_ms": 0.0, "ttft_ms": 9.0, "total_ms": 17.0}
    assert before == {
        "at_ms": 0.0,
        "resolution_ms": 1000.0,
        "window_ms": 900_000.0,
        "models": {
            "standard": {
                "traffic": {
                    "available": True,
                    "in_flight": 0,
                    "arrived_total": 0,
                    "completed_total": 0,
                    "output_tokens_total": 0,
                    "queue_full_total": 0,
                    "timeout_total": 0,
                    "downgrade_total": 0,
                },
                "queue": {"available": True, "length": 0},
                "batch": {"available": True, "size": 0, "max_size": 4},
                "kv": {"available": True, "used_tokens": 0, "capacity_tokens": 1024},
                "cache": {
                    "available": True,
                    "size_tokens": 0,
                    "lookups_total": 0,
                    "hits_total": 0,
                    "prompt_tokens_total": 0,
                    "cached_tokens_total": 0,
                    # Nothing has been looked up, so there is no rate: null, not 0.
                    "hit_rate": None,
                    "token_hit_rate": None,
                },
                "latency": {
                    "available": True,
                    "window_ms": 300_000.0,
                    "ttft_ms": None,
                    "itl_ms": None,
                    "total_ms": None,
                },
                "slo": NO_SLO,
                "series": [_point()],
            }
        },
        "tenants": {"available": False, "tenants": None},
        "events": {
            "available": True,
            "capacity": 1000,
            "retained": 0,
            "last_seq": 0,
            "counts": NO_EVENTS,
            "recent": [],
        },
    }
    # Prefill ends at 5 ms; the tokens come at 9, 13 and 17 ms.
    assert after["at_ms"] == 17.0
    assert after["models"]["standard"] == {
        "traffic": {
            "available": True,
            "in_flight": 0,
            "arrived_total": 1,
            "completed_total": 1,
            "output_tokens_total": 3,
            "queue_full_total": 0,
            "timeout_total": 0,
            "downgrade_total": 0,
        },
        "queue": {"available": True, "length": 0},
        "batch": {"available": True, "size": 0, "max_size": 4},
        # The finished request's ten prompt tokens are now in the prefix cache.
        "kv": {"available": True, "used_tokens": 10, "capacity_tokens": 1024},
        "cache": {
            "available": True,
            "size_tokens": 10,
            "lookups_total": 1,
            "hits_total": 0,
            "prompt_tokens_total": 10,
            "cached_tokens_total": 0,
            "hit_rate": 0.0,
            "token_hit_rate": 0.0,
        },
        "latency": {
            "available": True,
            "window_ms": 300_000.0,
            "ttft_ms": {"p50": 9.0, "p99": 9.0, "samples": 1},
            "itl_ms": {"p50": 4.0, "p99": 4.0, "samples": 2},
            "total_ms": {"p50": 17.0, "p99": 17.0, "samples": 1},
        },
        "slo": NO_SLO,
        "series": [
            _point(
                arrived=1,
                completed=1,
                req_per_s=1.0,
                output_tokens=3,
                tokens_per_s=3.0,
                in_flight_max=1,
                batch_size_max=1,
                # While it ran it held 10 prompt + 3 output tokens; 10 stayed, cached.
                kv_used_tokens=10,
                kv_used_tokens_max=13,
                cache_lookups=1,
                cache_hit_rate=0.0,
                cache_token_hit_rate=0.0,
                ttft_p50_ms=9.0,
                ttft_p99_ms=9.0,
                itl_p50_ms=4.0,
                itl_p99_ms=4.0,
                total_p50_ms=17.0,
                total_p99_ms=17.0,
            )
        ],
    }
    assert after["events"]["last_seq"] == 0


async def test_a_repeated_prompt_shows_as_a_cache_hit() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        await gateway.generate(model="standard", prompt=PROMPT)
        repeat = await gateway.generate(model="standard", prompt=PROMPT)
        standard = (await gateway.snapshot())["models"]["standard"]

    # All ten prompt tokens were cached, so there was no prefill: 3 * 4 = 12 ms.
    assert repeat.json()["timing"] == {"queue_ms": 0.0, "ttft_ms": 4.0, "total_ms": 12.0}
    assert standard["cache"] == {
        "available": True,
        "size_tokens": 10,
        "lookups_total": 2,
        "hits_total": 1,
        "prompt_tokens_total": 20,
        "cached_tokens_total": 10,
        "hit_rate": 0.5,
        "token_hit_rate": 0.5,
    }
    (point,) = standard["series"]
    assert (point["cache_lookups"], point["cache_hit_rate"], point["cache_token_hit_rate"]) == (
        2,
        0.5,
        0.5,
    )
    # Two requests in the step: nearest-rank p50 is the faster, p99 the slower.
    assert (point["ttft_p50_ms"], point["ttft_p99_ms"]) == (4.0, 9.0)
    assert (point["total_p50_ms"], point["total_p99_ms"]) == (12.0, 17.0)
    # The second request held only its 3 output tokens beside the 10 cached ones.
    assert (point["kv_used_tokens"], point["kv_used_tokens_max"]) == (10, 13)


async def test_a_burst_shows_queue_batch_kv_and_the_rejected_request() -> None:
    clock = SimClock()
    async with _gateway(_single(clock, max_queue_len=1), clock) as gateway:
        responses = await asyncio.gather(
            *(gateway.generate(model="standard", prompt=_prompt(f"p{i}")) for i in range(6))
        )
        snapshot = await gateway.snapshot()

    # The batch seats four and one may wait: the sixth is refused.
    assert [r.status_code for r in responses] == [200] * 5 + [429]
    # Four prefills of 5 ms, then steps of 4.75 ms for a batch of four: tokens at 24.75,
    # 29.5 and 34.25 ms. The fifth is then prefilled alone and gets its tokens at 43.25,
    # 47.25 and 51.25 ms.
    assert [r.json()["timing"]["total_ms"] for r in responses[:5]] == [34.25] * 4 + [51.25]
    assert snapshot["at_ms"] == 51.25
    standard = snapshot["models"]["standard"]
    assert standard["traffic"] == {
        "available": True,
        "in_flight": 0,
        "arrived_total": 6,
        "completed_total": 5,
        "output_tokens_total": 15,
        "queue_full_total": 1,
        "timeout_total": 0,
        "downgrade_total": 0,
    }
    assert standard["kv"]["used_tokens"] == 50  # five prompts of ten tokens, all cached
    assert standard["series"] == [
        _point(
            arrived=6,
            completed=5,
            req_per_s=6.0,
            output_tokens=15,
            tokens_per_s=15.0,
            queue_full=1,
            # The refused request was with the worker for an instant.
            in_flight_max=6,
            queue_len_max=1,
            batch_size_max=4,
            kv_used_tokens=50,
            # Four prompts cached (40) while the fifth ran with its 13 tokens.
            kv_used_tokens_max=53,
            cache_lookups=5,
            cache_hit_rate=0.0,
            cache_token_hit_rate=0.0,
            # TTFTs: 24.75 four times and 43.25. Totals: 34.25 four times and 51.25.
            ttft_p50_ms=24.75,
            ttft_p99_ms=43.25,
            # Ten gaps: eight of 4.75 ms and two of 4 ms.
            itl_p50_ms=4.75,
            itl_p99_ms=4.75,
            total_p50_ms=34.25,
            total_p99_ms=51.25,
        )
    ]
    assert snapshot["events"] == {
        "available": True,
        "capacity": 1000,
        "retained": 1,
        "last_seq": 1,
        "counts": {**NO_EVENTS, "queue_full": 1},
        "recent": [
            {
                "seq": 1,
                "at_ms": 0.0,
                "type": "queue_full",
                "model": "standard",
                "tenant_id": None,
                "data": {"waiting": 1, "limit": 1},
            }
        ],
    }


async def test_series_has_one_point_per_second_with_activity_and_history_selects_them() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        await gateway.generate(model="standard", prompt=_prompt("a"))
        await clock.sleep_ms(2500.0 - 17.0)
        await gateway.generate(model="standard", prompt=_prompt("b"))
        now_only = await gateway.snapshot()
        with_history = await gateway.snapshot(history_s=10)
        one_second = await gateway.snapshot(history_s=1)

    assert now_only["at_ms"] == 2517.0
    series = with_history["models"]["standard"]["series"]
    # Nothing happened between 1000 and 2000 ms and nobody looked: that second is a gap.
    assert [(p["t_ms"], p["final"], p["arrived"]) for p in series] == [
        (0.0, True, 1),
        (2000.0, False, 1),
    ]
    assert [p["total_p99_ms"] for p in series] == [17.0, 17.0]
    assert now_only["models"]["standard"]["series"] == series[1:]
    assert one_second["models"]["standard"]["series"] == series[1:]
    # The totals are since the start, whatever history was asked for.
    assert now_only["models"]["standard"]["traffic"]["completed_total"] == 2


# --- unavailable is not zero ---------------------------------------------------------------


async def test_a_worker_without_a_scheduler_view_reports_those_groups_unavailable() -> None:
    clock = SimClock()
    registry = ModelRegistry()
    registry.register("standard", _worker(clock, cache=False))
    registry.register(
        "slow",
        BaselineWorker(
            EXACT, SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30), clock
        ),
    )
    async with _gateway(registry, clock) as gateway:
        await gateway.generate(model="slow", prompt=PROMPT)
        models = (await gateway.snapshot())["models"]

    slow = models["slow"]
    assert {group: slow[group] for group in UNAVAILABLE} == UNAVAILABLE
    # What the gateway measures itself is there for every worker.
    assert slow["traffic"]["available"] is True
    assert (slow["traffic"]["completed_total"], slow["traffic"]["output_tokens_total"]) == (1, 3)
    assert slow["latency"]["total_ms"] == {"p50": 17.0, "p99": 17.0, "samples": 1}
    (point,) = slow["series"]
    assert all(point[name] is None for name in SCHEDULER_FIELDS)
    assert (point["completed"], point["output_tokens"], point["in_flight_max"]) == (1, 3, 1)
    assert (point["ttft_p50_ms"], point["total_p99_ms"]) == (9.0, 17.0)

    # A continuous batching worker built without a prefix cache has everything but that.
    standard = models["standard"]
    assert standard["queue"] == {"available": True, "length": 0}
    assert standard["batch"] == {"available": True, "size": 0, "max_size": 4}
    assert standard["kv"] == {"available": True, "used_tokens": 0, "capacity_tokens": 1024}
    assert standard["cache"] == UNAVAILABLE["cache"]
    (point,) = standard["series"]
    assert (point["queue_len"], point["batch_size"], point["kv_used_tokens"]) == (0, 0, 0)
    assert (point["cache_lookups"], point["cache_hit_rate"]) == (None, None)


async def test_passthrough_models_report_scheduler_metrics_unavailable_not_zero() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    config = BackendConfig(
        kind="llamacpp",
        servers={tier: LlamaServer(f"http://127.0.0.1:808{i}") for i, tier in enumerate(Tier, 1)},
    )
    registry = build_llamacpp_registry(config, clock, client=server.client())
    assert all(isinstance(registry.get(name), PassthroughWorker) for name in registry.names)
    assert not any(isinstance(registry.get(name), SchedulerView) for name in registry.names)

    async with _gateway(registry, clock, plans=load_plans()) as gateway:
        response = await gateway.generate(
            tenant_id="acme", model="premium", prompt="Say hello to the world"
        )
        snapshot = await gateway.snapshot()
        text = await gateway.metrics()

    assert response.status_code == 200
    for model in snapshot["models"].values():
        assert {group: model[group] for group in UNAVAILABLE} == UNAVAILABLE
        assert all(point[name] is None for point in model["series"] for name in SCHEDULER_FIELDS)

    premium = snapshot["models"]["premium"]
    # The mock server sends three pieces, at 50, 60 and 70 ms.
    assert premium["traffic"] == {
        "available": True,
        "in_flight": 0,
        "arrived_total": 1,
        "completed_total": 1,
        "output_tokens_total": 3,
        "queue_full_total": 0,
        "timeout_total": 0,
        "downgrade_total": 0,
    }
    assert premium["latency"]["ttft_ms"] == {"p50": 50.0, "p99": 50.0, "samples": 1}
    (point,) = premium["series"]
    assert (point["arrived"], point["completed"], point["output_tokens"]) == (1, 1, 3)
    assert (point["in_flight"], point["in_flight_max"]) == (0, 1)
    assert (point["ttft_p50_ms"], point["itl_p50_ms"], point["total_p50_ms"]) == (50.0, 10.0, 70.0)
    # A model nobody used still has a point, because the snapshot looked at it.
    (idle,) = snapshot["models"]["economy"]["series"]
    assert (idle["arrived"], idle["in_flight"], idle["queue_len"]) == (0, 0, None)

    # Prometheus: the series are absent, and a gauge says why.
    for name in ("queue_length", "batch_size", "batch_max_size", "kv_used_tokens", "cache_"):
        assert f"gateway_{name}" not in text
    assert "gateway_kv_capacity_tokens" not in text
    for group in ("queue", "batch", "kv", "cache", "slo"):
        assert f'gateway_metric_group_available{{model="premium",group="{group}"}} 0' in text
    for group in ("traffic", "latency"):
        assert f'gateway_metric_group_available{{model="premium",group="{group}"}} 1' in text
    assert 'gateway_requests_completed_total{model="premium"} 1' in text
    assert 'gateway_ttft_ms{model="premium",quantile="0.99"} 50.0' in text


def test_only_a_worker_with_its_own_scheduler_matches_the_view() -> None:
    clock = SimClock()
    assert isinstance(_worker(clock), SchedulerView)
    assert not isinstance(
        BaselineWorker(EXACT, SimulatedBackend(seed=1), clock),
        SchedulerView,
    )
    assert not isinstance(
        PassthroughWorker(EXACT, SimulatedBackend(seed=1), clock),
        SchedulerView,
    )


# --- tenants -------------------------------------------------------------------------------

PLANS = PlansConfig(
    plans={"basic": Plan("basic", ("standard",), 2.0, 30)},
    tenants={"t1": "basic", "t2": "basic"},
)


async def test_tenants_show_spend_tokens_against_budget_and_rejections() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock, plans=PLANS) as gateway:
        served = [
            await gateway.generate(tenant_id="t1", model="standard", prompt=_prompt(word))
            for word in "abc"
        ]
        rejected = await gateway.generate(tenant_id="t1", model="standard", prompt=_prompt("d"))
        unknown = await gateway.generate(tenant_id="nobody", model="standard", prompt=PROMPT)
        snapshot = await gateway.snapshot()

    # Each request bills 13 tokens against a budget of 30: the check passes at 0, 13 and
    # 26 used, and fails at 39.
    assert [r.status_code for r in served] == [200, 200, 200]
    assert (rejected.status_code, rejected.json()["error"]["code"]) == (429, "budget_exhausted")
    assert unknown.status_code == 404
    billed = COST * 2.0
    assert snapshot["tenants"] == {
        "available": True,
        "tenants": {
            "t1": {
                "plan": "basic",
                "period_start_ms": 0.0,
                "period_end_ms": 30 * 24 * 60 * 60 * 1000.0,
                "budget_tokens": 30,
                "used_tokens": 39,
                "remaining_tokens": 0,
                "spend_usd": billed + billed + billed,
                "requests": 3,
                "rejections_total": {"budget_exhausted": 1, "queue_full": 0},
                "timeout_total": 0,
                "downgrade_total": 0,
                "series": [
                    {
                        "t_ms": 0.0,
                        "final": False,
                        "requests": 3,
                        "billed_tokens": 39,
                        "spend_usd": billed + billed + billed,
                        "rejections": 1,
                        "timeouts": 0,
                        "downgrades": 0,
                    }
                ],
            },
            # Configured, so listed; unused, so empty. The unknown tenant is not listed.
            "t2": {
                "plan": "basic",
                "period_start_ms": 0.0,
                "period_end_ms": 30 * 24 * 60 * 60 * 1000.0,
                "budget_tokens": 30,
                "used_tokens": 0,
                "remaining_tokens": 30,
                "spend_usd": 0.0,
                "requests": 0,
                "rejections_total": {"budget_exhausted": 0, "queue_full": 0},
                "timeout_total": 0,
                "downgrade_total": 0,
                "series": [],
            },
        },
    }
    # Three requests of 17 ms each, one after another.
    assert snapshot["events"]["recent"] == [
        {
            "seq": 1,
            "at_ms": 51.0,
            "type": "budget_exhausted",
            "model": "standard",
            "tenant_id": "t1",
            "data": {"used_tokens": 39, "budget_tokens": 30},
        }
    ]
    # The request refused for its budget never reached the model.
    assert snapshot["models"]["standard"]["traffic"]["arrived_total"] == 3


async def test_a_full_queue_counts_against_the_tenant_that_was_refused() -> None:
    clock = SimClock()
    async with _gateway(_single(clock, max_queue_len=0), clock, plans=PLANS) as gateway:
        responses = await asyncio.gather(
            *(
                gateway.generate(tenant_id="t1", model="standard", prompt=_prompt(f"p{i}"))
                for i in range(5)
            )
        )
        snapshot = await gateway.snapshot()

    assert [r.status_code for r in responses] == [200, 200, 200, 200, 429]
    t1 = snapshot["tenants"]["tenants"]["t1"]
    assert t1["rejections_total"] == {"budget_exhausted": 0, "queue_full": 1}
    assert (t1["series"][0]["requests"], t1["series"][0]["rejections"]) == (4, 1)
    assert snapshot["events"]["recent"][0]["tenant_id"] == "t1"


# --- timeouts, downgrades, state changes ---------------------------------------------------


async def test_a_timeout_is_counted_and_published_with_the_tokens_it_produced() -> None:
    clock = SimClock()
    policy = OverloadPolicy(request_timeout_ms=14.0)
    async with _gateway(_single(clock), clock, overload=policy) as gateway:
        # Thirty tokens would take 125 ms; it is cancelled at 14 ms, after two of them.
        response = await gateway.generate(model="standard", prompt=PROMPT, max_tokens=30)
        snapshot = await gateway.snapshot()
        await clock.sleep_ms(3.0)
        later = await gateway.snapshot()

    assert response.status_code == 504
    assert snapshot["at_ms"] == 14.0
    standard = snapshot["models"]["standard"]
    assert standard["traffic"] == {
        "available": True,
        "in_flight": 0,
        "arrived_total": 1,
        "completed_total": 0,
        "output_tokens_total": 2,
        "queue_full_total": 0,
        "timeout_total": 1,
        "downgrade_total": 0,
    }
    (point,) = standard["series"]
    assert (point["timeouts"], point["completed"], point["output_tokens"]) == (1, 0, 2)
    assert (point["ttft_p50_ms"], point["total_p50_ms"]) == (None, None)
    # The worker lets go of a cancelled request at its next step boundary, 17 ms: until
    # then its 40 KV tokens are still held, though it no longer counts as seated.
    assert (standard["batch"]["size"], standard["kv"]["used_tokens"]) == (0, 40)
    assert later["models"]["standard"]["kv"]["used_tokens"] == 0
    assert snapshot["events"]["recent"] == [
        {
            "seq": 1,
            "at_ms": 14.0,
            "type": "timeout",
            "model": "standard",
            "tenant_id": None,
            "data": {"timeout_ms": 14.0},
        }
    ]


async def test_degradation_downgrade_and_recovery_show_in_flags_counters_and_events() -> None:
    clock = SimClock()
    registry = ModelRegistry()
    registry.register("small", _worker(clock))
    registry.register("big", _worker(clock))
    # Any finished request slower than 10 ms degrades "big"; "small" has no SLO.
    policy = OverloadPolicy(
        slo=SloPolicy(p99_ms={"big": 10.0}, window_ms=10_000.0, min_samples=1, min_hold_ms=0.0)
    )
    async with _gateway(registry, clock, overload=policy) as gateway:
        healthy = await gateway.snapshot()
        await gateway.generate(model="big", prompt=_prompt("a"))
        moved = await gateway.generate(model="big", prompt=_prompt("b"), allow_downgrade=True)
        degraded = await gateway.snapshot()
        await clock.sleep_ms(10_000.0)
        recovered = await gateway.snapshot(history_s=60)

    assert moved.headers["X-Downgraded-From"] == "big"
    assert healthy["models"]["big"]["slo"] == {
        "available": True,
        "degraded": False,
        "slo_p99_ms": 10.0,
        "recent_p99_ms": None,
        "samples": 0,
    }
    assert healthy["models"]["big"]["series"][0]["degraded"] is False
    # A model without an SLO has no degraded flag at all.
    assert healthy["models"]["small"]["slo"] == NO_SLO
    assert healthy["models"]["small"]["series"][0]["degraded"] is None

    # The first request took 17 ms; the second was served by "small" from 17 to 34 ms.
    assert degraded["at_ms"] == 34.0
    assert degraded["models"]["big"]["slo"] == {
        "available": True,
        "degraded": True,
        "slo_p99_ms": 10.0,
        "recent_p99_ms": 17.0,
        "samples": 1,
    }
    assert degraded["models"]["big"]["traffic"]["downgrade_total"] == 1
    (point,) = degraded["models"]["big"]["series"]
    assert (point["degraded"], point["downgrades"], point["arrived"]) == (True, 1, 1)
    assert degraded["models"]["small"]["traffic"]["completed_total"] == 1
    assert degraded["events"]["counts"] == {**NO_EVENTS, "state_change": 1, "downgrade": 1}
    assert degraded["events"]["recent"] == [
        {
            "seq": 1,
            "at_ms": 17.0,
            "type": "state_change",
            "model": "big",
            "tenant_id": None,
            "data": {"degraded": True, "p99_ms": 17.0},
        },
        {
            "seq": 2,
            "at_ms": 17.0,
            "type": "downgrade",
            "model": "big",
            "tenant_id": None,
            "data": {"to_model": "small"},
        },
    ]

    # Ten seconds on, the slow request has left the SLO window. Taking the snapshot asks
    # the SLO monitor, which notices; that is when the change is stamped.
    assert recovered["at_ms"] == 10_034.0
    assert recovered["models"]["big"]["slo"]["degraded"] is False
    assert [(p["t_ms"], p["degraded"]) for p in recovered["models"]["big"]["series"]] == [
        (0.0, True),
        (10_000.0, False),
    ]
    assert recovered["events"]["recent"][-1] == {
        "seq": 3,
        "at_ms": 10_034.0,
        "type": "state_change",
        "model": "big",
        "tenant_id": None,
        "data": {"degraded": False, "p99_ms": None},
    }


# --- the stream ----------------------------------------------------------------------------


async def test_stream_pushes_a_snapshot_every_second_and_the_events_in_between() -> None:
    clock = SimClock()
    async with _gateway(_single(clock, max_queue_len=1), clock) as gateway:
        stream = asyncio.ensure_future(gateway.client.get("/v1/monitor/stream?count=3"))
        await clock.sleep_ms(500.0)
        # The burst of the test above, half a second in: one request is refused.
        await asyncio.gather(
            *(gateway.generate(model="standard", prompt=_prompt(f"p{i}")) for i in range(6))
        )
        response = await stream
        finished_at = clock.now_ms()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert finished_at == 2000.0
    events = _sse(response)
    assert [(name, event_id) for name, _, event_id in events] == [
        ("snapshot", None),
        ("event", "1"),
        ("snapshot", None),
        ("snapshot", None),
    ]
    first, (_, event, _), second, third = events
    assert [snapshot["at_ms"] for _, snapshot, _ in (first, second, third)] == [0.0, 1000.0, 2000.0]
    # The event comes just before the first snapshot taken after it happened.
    assert event == {
        "seq": 1,
        "at_ms": 500.0,
        "type": "queue_full",
        "model": "standard",
        "tenant_id": None,
        "data": {"waiting": 1, "limit": 1},
    }
    assert first[1]["models"]["standard"]["series"] == [_point()]
    # Each later push carries the second that just ended, now final, and the new one.
    assert second[1]["models"]["standard"]["series"] == [
        _point(
            final=True,
            arrived=6,
            completed=5,
            req_per_s=6.0,
            output_tokens=15,
            tokens_per_s=15.0,
            queue_full=1,
            in_flight_max=6,
            queue_len_max=1,
            batch_size_max=4,
            kv_used_tokens=50,
            kv_used_tokens_max=53,
            cache_lookups=5,
            cache_hit_rate=0.0,
            cache_token_hit_rate=0.0,
            ttft_p50_ms=24.75,
            ttft_p99_ms=43.25,
            itl_p50_ms=4.75,
            itl_p99_ms=4.75,
            total_p50_ms=34.25,
            total_p99_ms=51.25,
        ),
        _point(t_ms=1000.0, kv_used_tokens=50, kv_used_tokens_max=50),
    ]
    assert third[1]["models"]["standard"]["series"] == [
        _point(t_ms=1000.0, final=True, kv_used_tokens=50, kv_used_tokens_max=50),
        _point(t_ms=2000.0, kv_used_tokens=50, kv_used_tokens_max=50),
    ]
    assert second[1]["events"]["counts"]["queue_full"] == 1
    assert second[1]["events"]["recent"] == []


async def test_first_stream_snapshot_is_the_snapshot_endpoint_with_the_same_history() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        await gateway.generate(model="standard", prompt=PROMPT)
        await clock.sleep_ms(3000.0)
        await gateway.generate(model="standard", prompt=PROMPT)
        snapshot = await gateway.snapshot(history_s=60, events=0)
        response = await gateway.client.get("/v1/monitor/stream?count=1&history_s=60")

    ((name, pushed, _),) = _sse(response)
    assert name == "snapshot"
    assert pushed == snapshot
    assert [p["t_ms"] for p in pushed["models"]["standard"]["series"]] == [0.0, 3000.0]


async def test_a_reconnecting_stream_resumes_after_the_last_event_it_saw() -> None:
    clock = SimClock()
    async with _gateway(
        _single(clock, max_queue_len=0), clock, events=EventBus(clock, max_events=2)
    ) as gateway:
        # Four are seated and three refused: events 1, 2 and 3. The log keeps two.
        await asyncio.gather(
            *(gateway.generate(model="standard", prompt=_prompt(f"p{i}")) for i in range(7))
        )

        async def stream(last_event_id: str | None) -> list[tuple[str, Any]]:
            headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
            response = await gateway.client.get("/v1/monitor/stream?count=1", headers=headers)
            return [
                (name, data["seq"] if name == "event" else data)
                for name, data, _ in _sse(response)
                if name != "snapshot"
            ]

        fresh = await stream(None)
        caught_up = await stream("3")
        one_behind = await stream("2")
        too_far_behind = await stream("0")
        not_a_number = await stream("abc")

    # A new client gets only what happens from now on.
    assert fresh == []
    assert caught_up == []
    assert one_behind == [("event", 3)]
    # Event 1 is gone from the log, and the client is told so.
    assert too_far_behind == [("events_missed", {"missed": 1}), ("event", 2), ("event", 3)]
    assert not_a_number == []


# --- Prometheus ----------------------------------------------------------------------------

EXPECTED_METRICS = """\
# HELP gateway_metric_group_available 1 if the gateway can observe this group of metrics \
for the model, else 0.
# TYPE gateway_metric_group_available gauge
gateway_metric_group_available{model="standard",group="traffic"} 1
gateway_metric_group_available{model="standard",group="queue"} 1
gateway_metric_group_available{model="standard",group="batch"} 1
gateway_metric_group_available{model="standard",group="kv"} 1
gateway_metric_group_available{model="standard",group="cache"} 1
gateway_metric_group_available{model="standard",group="latency"} 1
gateway_metric_group_available{model="standard",group="slo"} 0
# HELP gateway_requests_arrived_total Requests handed to the model's worker, including \
ones it refused.
# TYPE gateway_requests_arrived_total counter
gateway_requests_arrived_total{model="standard"} 1
# HELP gateway_requests_completed_total Requests that finished successfully.
# TYPE gateway_requests_completed_total counter
gateway_requests_completed_total{model="standard"} 1
# HELP gateway_output_tokens_total Output tokens produced, including those of requests \
that did not finish.
# TYPE gateway_output_tokens_total counter
gateway_output_tokens_total{model="standard"} 3
# HELP gateway_queue_full_total Requests refused with 429 because the model's queue was full.
# TYPE gateway_queue_full_total counter
gateway_queue_full_total{model="standard"} 0
# HELP gateway_timeouts_total Requests cancelled with 504 at the request timeout.
# TYPE gateway_timeouts_total counter
gateway_timeouts_total{model="standard"} 0
# HELP gateway_downgrades_total Requests moved from this model to a cheaper one.
# TYPE gateway_downgrades_total counter
gateway_downgrades_total{model="standard"} 0
# HELP gateway_in_flight_requests Requests currently with the model's worker.
# TYPE gateway_in_flight_requests gauge
gateway_in_flight_requests{model="standard"} 0
# HELP gateway_queue_length Requests waiting for a batch slot.
# TYPE gateway_queue_length gauge
gateway_queue_length{model="standard"} 0
# HELP gateway_batch_size Requests seated in the running batch.
# TYPE gateway_batch_size gauge
gateway_batch_size{model="standard"} 0
# HELP gateway_batch_max_size Most requests the batch can seat.
# TYPE gateway_batch_max_size gauge
gateway_batch_max_size{model="standard"} 4
# HELP gateway_kv_used_tokens KV tokens held by running requests and the prefix cache.
# TYPE gateway_kv_used_tokens gauge
gateway_kv_used_tokens{model="standard"} 10
# HELP gateway_kv_capacity_tokens KV tokens the model can hold.
# TYPE gateway_kv_capacity_tokens gauge
gateway_kv_capacity_tokens{model="standard"} 1024
# HELP gateway_cache_lookups_total Prefix cache lookups.
# TYPE gateway_cache_lookups_total counter
gateway_cache_lookups_total{model="standard"} 1
# HELP gateway_cache_hits_total Prefix cache lookups that matched at least one token.
# TYPE gateway_cache_hits_total counter
gateway_cache_hits_total{model="standard"} 0
# HELP gateway_cache_prompt_tokens_total Prompt tokens looked up in the prefix cache.
# TYPE gateway_cache_prompt_tokens_total counter
gateway_cache_prompt_tokens_total{model="standard"} 10
# HELP gateway_cache_cached_tokens_total Looked-up prompt tokens that were already cached.
# TYPE gateway_cache_cached_tokens_total counter
gateway_cache_cached_tokens_total{model="standard"} 0
# HELP gateway_ttft_ms Time to first token over the metrics window, nearest-rank, in ms.
# TYPE gateway_ttft_ms gauge
gateway_ttft_ms{model="standard",quantile="0.5"} 9.0
gateway_ttft_ms{model="standard",quantile="0.99"} 9.0
# HELP gateway_itl_ms Inter-token latency over the metrics window, nearest-rank, in ms.
# TYPE gateway_itl_ms gauge
gateway_itl_ms{model="standard",quantile="0.5"} 4.0
gateway_itl_ms{model="standard",quantile="0.99"} 4.0
# HELP gateway_total_latency_ms Total latency over the metrics window, nearest-rank, in ms.
# TYPE gateway_total_latency_ms gauge
gateway_total_latency_ms{model="standard",quantile="0.5"} 17.0
gateway_total_latency_ms{model="standard",quantile="0.99"} 17.0
# HELP gateway_events_total Events published on the gateway's event bus, by type.
# TYPE gateway_events_total counter
gateway_events_total{type="state_change"} 0
gateway_events_total{type="queue_full"} 0
gateway_events_total{type="timeout"} 0
gateway_events_total{type="downgrade"} 0
gateway_events_total{type="budget_exhausted"} 0
"""

SAMPLE_LINE = re.compile(r'^[a-z_]+(\{[a-z]+="[^"]*"(,[a-z]+="[^"]*")*\})? -?[0-9.e+-]+$')


async def test_metrics_endpoint_is_exact_prometheus_text_for_one_request() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        await gateway.generate(model="standard", prompt=PROMPT)
        response = await gateway.client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    # No SLO and no tenants here, so gateway_degraded and gateway_tenant_* are absent.
    assert response.text == EXPECTED_METRICS


async def test_metrics_endpoint_covers_tenants_slo_and_every_line_is_well_formed() -> None:
    clock = SimClock()
    policy = OverloadPolicy(slo=SloPolicy(p99_ms={"standard": 10.0}, min_samples=1))
    async with _gateway(
        _single(clock, max_queue_len=0), clock, plans=PLANS, overload=policy
    ) as gateway:
        await asyncio.gather(
            *(
                gateway.generate(tenant_id="t1", model="standard", prompt=_prompt(f"p{i}"))
                for i in range(5)
            )
        )
        text = await gateway.metrics()

    lines = text.splitlines()
    assert text.endswith("\n")
    samples = [line for line in lines if not line.startswith("#")]
    assert all(SAMPLE_LINE.match(line) for line in samples), samples
    # Every family is introduced by its HELP and TYPE lines, once.
    names = {line.split("{")[0].split(" ")[0] for line in samples}
    for name in names:
        assert sum(1 for line in lines if line.startswith(f"# HELP {name} ")) == 1
        assert sum(1 for line in lines if line.startswith(f"# TYPE {name} ")) == 1
    # Four requests of 13 billed tokens at twice the base cost; the fifth was refused.
    assert 'gateway_degraded{model="standard"} 1' in samples
    assert 'gateway_metric_group_available{model="standard",group="slo"} 1' in samples
    assert 'gateway_tenant_used_tokens{tenant="t1"} 52' in samples
    assert 'gateway_tenant_budget_tokens{tenant="t1"} 30' in samples
    assert 'gateway_tenant_requests{tenant="t1"} 4' in samples
    assert f'gateway_tenant_spend_usd{{tenant="t1"}} {COST * 2 * 4!r}' in samples
    assert 'gateway_tenant_rejections_total{tenant="t1",reason="queue_full"} 1' in samples
    assert 'gateway_tenant_rejections_total{tenant="t1",reason="budget_exhausted"} 0' in samples
    assert 'gateway_tenant_requests{tenant="t2"} 0' in samples
    assert 'gateway_queue_full_total{model="standard"} 1' in samples
    assert 'gateway_events_total{type="queue_full"} 1' in samples
    assert 'gateway_events_total{type="state_change"} 1' in samples


def test_prometheus_label_values_are_escaped() -> None:
    clock = SimClock()
    registry = ModelRegistry()
    registry.register('we"ird\\name\n', _worker(clock))
    monitor = Monitor(
        clock,
        registry,
        MetricsStore(clock),
        series=TimeSeriesStore(clock),
        events=EventBus(clock),
    )

    text = render_prometheus(monitor.snapshot())

    assert 'gateway_in_flight_requests{model="we\\"ird\\\\name\\n"} 0' in text.splitlines()


# --- bounded memory ------------------------------------------------------------------------


async def test_memory_stays_bounded_through_the_api() -> None:
    clock = SimClock()
    series = TimeSeriesStore(clock, window_ms=5000.0)
    events = EventBus(clock, max_events=3)
    registry = ModelRegistry()
    registry.register("standard", _worker(clock, max_queue_len=0))
    registry.register("other", _worker(clock))
    plans = PlansConfig(
        plans={"big": Plan("big", ("standard", "other"), 1.0, 10**9)},
        tenants={"t1": "big", "t2": "big"},
    )
    async with _gateway(registry, clock, plans=plans, series=series, events=events) as gateway:
        most_buckets = 0
        for second in range(40):
            # Five at once into a batch of four with no queue: one refusal every second.
            await asyncio.gather(
                *(
                    gateway.generate(
                        tenant_id="t1", model="standard", prompt=_prompt(f"s{second}p{i}")
                    )
                    for i in range(5)
                )
            )
            # Names nobody configured must not create series.
            await gateway.generate(tenant_id=f"stranger{second}", model="standard", prompt=PROMPT)
            await gateway.generate(tenant_id="t1", model=f"model{second}", prompt=PROMPT)
            # A dashboard looking once a second, which gives the idle model its points.
            await gateway.snapshot()
            most_buckets = max(most_buckets, series.retained_buckets())
            await clock.sleep_ms(1000.0 - clock.now_ms() % 1000.0)
        snapshot = await gateway.snapshot(history_s=3600, events=1000)

    assert gateway.app.state.monitoring.series is series
    # Two models and the one tenant that did anything, five one-second buckets each.
    assert most_buckets == 3 * 5
    assert series.retained_samples() <= 2 * (2 * 1024 + 8192)
    assert snapshot["at_ms"] == 40_000.0
    standard = snapshot["models"]["standard"]["series"]
    # 40 seconds were recorded; the window keeps the last five (the newest is the
    # snapshot's own look at a second in which nothing has happened yet).
    assert [p["t_ms"] for p in standard] == [36_000.0, 37_000.0, 38_000.0, 39_000.0, 40_000.0]
    assert [p["arrived"] for p in standard] == [5, 5, 5, 5, 0]
    assert snapshot["models"]["standard"]["traffic"]["arrived_total"] == 200
    assert set(snapshot["models"]) == {"standard", "other"}
    assert set(snapshot["tenants"]["tenants"]) == {"t1", "t2"}
    assert len(snapshot["tenants"]["tenants"]["t1"]["series"]) == 4
    assert len(snapshot["models"]["other"]["series"]) == 5
    # Forty refusals were published; the log holds the last three and still counts all.
    assert snapshot["events"]["capacity"] == 3
    assert snapshot["events"]["retained"] == 3
    assert [e["seq"] for e in snapshot["events"]["recent"]] == [38, 39, 40]
    assert snapshot["events"]["counts"] == {**NO_EVENTS, "queue_full": 40}


# --- the monitor itself --------------------------------------------------------------------


def _finished(tokens_out: int) -> RequestResult:
    return RequestResult(
        request_id="r",
        model="m",
        enqueue_ms=0.0,
        start_ms=0.0,
        first_token_ms=0.0,
        finish_ms=0.0,
        tokens_in=4,
        tokens_out=tokens_out,
        cost_usd=0.0,
    )


def test_tokens_a_backend_did_not_report_one_by_one_are_added_at_completion() -> None:
    clock = SimClock()
    registry = ModelRegistry()
    registry.register("m", BaselineWorker(EXACT, SimulatedBackend(seed=1), clock))
    series = TimeSeriesStore(clock)
    monitor = Monitor(clock, registry, MetricsStore(clock), series=series, events=EventBus(clock))

    # One piece streamed, but the server's usage says three tokens: two are added.
    monitor.token("m")
    monitor.completed("m", None, _finished(3), billed_cost_usd=0.0, live_tokens=1)
    assert series.model_totals("m").output_tokens == 3

    # Every token streamed: nothing to add.
    for _ in range(3):
        monitor.token("m")
    monitor.completed("m", None, _finished(3), billed_cost_usd=0.0, live_tokens=3)
    assert series.model_totals("m").output_tokens == 6


# --- wiring --------------------------------------------------------------------------------


async def test_query_parameters_are_validated() -> None:
    clock = SimClock()
    async with _gateway(_single(clock), clock) as gateway:
        negative = await gateway.client.get("/v1/monitor/snapshot?history_s=-1")
        no_pushes = await gateway.client.get("/v1/monitor/stream?count=0")
        no_events = await gateway.snapshot(events=0)

    assert negative.status_code == 422
    assert [d["field"] for d in negative.json()["error"]["details"]] == ["history_s"]
    assert no_pushes.status_code == 422
    assert no_events["events"]["recent"] == []


async def test_default_app_reports_every_group_for_the_three_simulated_models() -> None:
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        snapshot = (await client.get("/v1/monitor/snapshot")).json()
        metrics = await client.get("/metrics")

    assert list(snapshot["models"]) == ["economy", "standard", "premium"]
    for model in snapshot["models"].values():
        assert all(
            model[group]["available"]
            for group in ("traffic", "queue", "batch", "kv", "cache", "latency", "slo")
        )
        assert model["slo"]["degraded"] is False
    assert snapshot["tenants"]["available"] is True
    assert set(snapshot["tenants"]["tenants"]) == {"initech", "globex", "acme"}
    assert (snapshot["resolution_ms"], snapshot["window_ms"]) == (1000.0, 900_000.0)
    assert metrics.status_code == 200
