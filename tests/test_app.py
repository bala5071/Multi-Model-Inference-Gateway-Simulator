from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from gateway.app import create_app, create_default_app
from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, StepRecord, build_continuous_registry
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest
from helpers import EXACT

# Ten words: 10 prompt tokens, so 5 ms of prefill on the EXACT profile.
PROMPT = "one two three four five six seven eight nine ten"


@dataclass
class Gateway:
    """An app on simulated time with one model, "standard", on the EXACT test profile."""

    client: httpx.AsyncClient
    clock: SimClock
    registry: ModelRegistry
    steps: list[StepRecord] = field(default_factory=list)

    async def generate(self, **body: Any) -> httpx.Response:
        return await self.client.post("/v1/generate", json=body)


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    clock = SimClock()
    steps: list[StepRecord] = []
    registry = ModelRegistry()
    registry.register(
        "standard",
        ContinuousBatchingWorker(
            EXACT,
            SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30),
            clock,
            on_step=steps.append,
        ),
        aliases=["exact-test-model"],
    )
    registry.register(
        "slow",
        BaselineWorker(
            EXACT, SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30), clock
        ),
    )
    app = create_app(registry, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, registry, steps)
    await registry.aclose()


# --- POST /v1/generate ---------------------------------------------------------------------


async def test_generate_happy_path(gateway: Gateway) -> None:
    response = await gateway.generate(
        model="standard", prompt=PROMPT, max_tokens=3, tenant_id="acme"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    # Prefill 10 * 0.5 = 5 ms, then three 4 ms decode steps.
    assert response.json() == {
        "id": "req-1",
        "text": "[simulated output, 3 tokens]",
        "model": "standard",
        "usage": {
            "prompt_tokens": 10,
            "cached_tokens": 0,
            "completion_tokens": 3,
            "total_tokens": 13,
        },
        "timing": {"queue_ms": 0.0, "ttft_ms": 9.0, "total_ms": 17.0},
        "cost": {"usd": (10 * 0.5 + 3 * 1.0) / 1000},
    }
    assert gateway.clock.now_ms() == 17.0


async def test_generate_accepts_an_alias_and_reports_the_registered_name(
    gateway: Gateway,
) -> None:
    response = await gateway.generate(model="exact-test-model", prompt=PROMPT, max_tokens=1)
    assert response.status_code == 200
    assert response.json()["model"] == "standard"


async def test_max_tokens_is_optional_and_caps_the_output(gateway: Gateway) -> None:
    uncapped = await gateway.generate(model="standard", prompt=PROMPT)
    capped = await gateway.generate(model="standard", prompt=PROMPT, max_tokens=5)
    assert uncapped.json()["usage"]["completion_tokens"] == 30
    assert capped.json()["usage"]["completion_tokens"] == 5


async def test_request_ids_count_up(gateway: Gateway) -> None:
    first = await gateway.generate(model="standard", prompt=PROMPT, max_tokens=1)
    second = await gateway.generate(model="slow", prompt=PROMPT, max_tokens=1)
    assert (first.json()["id"], second.json()["id"]) == ("req-1", "req-2")


async def test_unknown_model_is_a_404_listing_the_valid_models(gateway: Gateway) -> None:
    response = await gateway.generate(model="gpt-4", prompt=PROMPT)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "unknown_model",
            "message": "Unknown model 'gpt-4'.",
            "valid_models": ["standard", "slow", "exact-test-model"],
        }
    }
    assert gateway.clock.now_ms() == 0.0  # nothing was run


@pytest.mark.parametrize(
    ("body", "field_name"),
    [
        ({"prompt": PROMPT}, "model"),
        ({"model": "standard"}, "prompt"),
        ({"model": "", "prompt": PROMPT}, "model"),
        ({"model": "standard", "prompt": ""}, "prompt"),
        ({"model": "standard", "prompt": 42}, "prompt"),
        ({"model": "standard", "prompt": PROMPT, "max_tokens": 0}, "max_tokens"),
        ({"model": "standard", "prompt": PROMPT, "max_tokens": -5}, "max_tokens"),
        ({"model": "standard", "prompt": PROMPT, "max_tokens": "many"}, "max_tokens"),
        ({"model": "standard", "prompt": PROMPT, "max_tokens": 1.5}, "max_tokens"),
        ({"model": "standard", "prompt": PROMPT, "tenant_id": ""}, "tenant_id"),
        ({"model": "standard", "prompt": PROMPT, "temperature": 0.2}, "temperature"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
async def test_bad_input_is_a_422_naming_the_field(
    gateway: Gateway, body: dict[str, Any], field_name: str
) -> None:
    response = await gateway.generate(**body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["valid_models"] == ["standard", "slow", "exact-test-model"]
    assert [d["field"] for d in error["details"]] == [field_name]
    assert all(d["message"] for d in error["details"])
    assert gateway.clock.now_ms() == 0.0


@pytest.mark.parametrize("content", [b"{not json", b"", b"[1, 2]", b'"just a string"'])
async def test_malformed_body_is_a_422_with_a_json_error(gateway: Gateway, content: bytes) -> None:
    response = await gateway.client.post(
        "/v1/generate", content=content, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_request_too_large_for_the_model_is_a_413(gateway: Gateway) -> None:
    huge_prompt = " ".join(f"w{i}" for i in range(EXACT.kv_capacity_tokens))
    response = await gateway.generate(model="standard", prompt=huge_prompt)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


# --- batching ------------------------------------------------------------------------------


async def test_concurrent_requests_to_the_same_model_are_batched(gateway: Gateway) -> None:
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]

    responses = await asyncio.gather(
        *(gateway.generate(model="standard", prompt=p, max_tokens=3) for p in prompts)
    )

    assert [r.status_code for r in responses] == [200, 200, 200]
    # Every decode step carried all three requests.
    assert [s.batch_size for s in gateway.steps] == [3, 3, 3]
    # Prefills run one after another (5 ms each), then three steps of 4.5 ms for the batch.
    timings = [r.json()["timing"] for r in responses]
    assert [t["queue_ms"] for t in timings] == [0.0, 5.0, 10.0]
    assert [t["total_ms"] for t in timings] == [28.5, 28.5, 28.5]
    assert gateway.clock.now_ms() == 28.5  # one at a time would take 3 * 17 = 51 ms


async def test_the_same_requests_on_the_baseline_worker_are_not_batched(
    gateway: Gateway,
) -> None:
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]

    responses = await asyncio.gather(
        *(gateway.generate(model="slow", prompt=p, max_tokens=3) for p in prompts)
    )

    assert [r.json()["timing"]["total_ms"] for r in responses] == [17.0, 34.0, 51.0]


async def test_requests_to_different_models_do_not_wait_for_each_other(gateway: Gateway) -> None:
    standard, slow = await asyncio.gather(
        gateway.generate(model="standard", prompt=PROMPT, max_tokens=3),
        gateway.generate(model="slow", prompt=PROMPT, max_tokens=3),
    )
    assert standard.json()["timing"] == {"queue_ms": 0.0, "ttft_ms": 9.0, "total_ms": 17.0}
    assert slow.json()["timing"] == {"queue_ms": 0.0, "ttft_ms": 9.0, "total_ms": 17.0}


# --- GET endpoints -------------------------------------------------------------------------


async def test_root_points_a_visitor_at_the_real_endpoints(gateway: Gateway) -> None:
    response = await gateway.client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["docs"] == "/docs"
    assert "POST /v1/generate" in body["endpoints"]
    # Every GET endpoint it lists (without a path parameter) really answers.
    for entry in body["endpoints"]:
        method, path = entry.split()
        if method == "GET" and "{" not in path:
            assert (await gateway.client.get(path)).status_code == 200, path


async def test_models_lists_names_and_aliases(gateway: Gateway) -> None:
    response = await gateway.client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {"id": "standard", "aliases": ["exact-test-model"]},
            {"id": "slow", "aliases": []},
        ]
    }


async def test_healthz_reports_ok_and_uptime_on_the_injected_clock(gateway: Gateway) -> None:
    before = await gateway.client.get("/healthz")
    await gateway.generate(model="standard", prompt=PROMPT, max_tokens=3)
    after = await gateway.client.get("/healthz")

    assert before.status_code == 200
    assert before.json() == {"status": "ok", "models": 2, "uptime_ms": 0.0}
    assert after.json() == {"status": "ok", "models": 2, "uptime_ms": 17.0}


async def test_metrics_are_empty_before_any_request(gateway: Gateway) -> None:
    response = await gateway.client.get("/metrics/summary")
    empty = {
        "requests": 0,
        "queue_ms": None,
        "ttft_ms": None,
        "total_ms": None,
        "itl_ms": None,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
    }
    assert response.status_code == 200
    assert response.json() == {"models": {"standard": empty, "slow": empty}}


async def test_metrics_reflect_the_requests_made(gateway: Gateway) -> None:
    # On "slow", three requests arriving together finish at 17, 34 and 51 ms.
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]
    await asyncio.gather(*(gateway.generate(model="slow", prompt=p, max_tokens=3) for p in prompts))
    await gateway.generate(model="exact-test-model", prompt=PROMPT, max_tokens=1)
    await gateway.generate(model="nope", prompt=PROMPT)  # 404: must not be counted
    await gateway.generate(model="slow", prompt="")  # 422: must not be counted

    metrics = (await gateway.client.get("/metrics/summary")).json()["models"]

    assert metrics["slow"] == {
        "requests": 3,
        "queue_ms": {"p50": 17.0, "p99": 34.0},
        "ttft_ms": {"p50": 26.0, "p99": 43.0},
        "total_ms": {"p50": 34.0, "p99": 51.0},
        # One at a time, every decode step is the 4 ms base step.
        "itl_ms": {"p50": 4.0, "p99": 4.0},
        "prompt_tokens": 30,
        "cached_tokens": 0,
        "completion_tokens": 9,
        "cost_usd": 3 * (10 * 0.5 + 3 * 1.0) / 1000,
    }
    # The request made through the alias is counted under the registered name.
    assert metrics["standard"]["requests"] == 1
    assert metrics["standard"]["total_ms"] == {"p50": 9.0, "p99": 9.0}
    assert metrics["standard"]["completion_tokens"] == 1


# --- wiring --------------------------------------------------------------------------------


async def test_prefix_cache_hits_show_up_in_usage_and_metrics() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        body = {"model": "llama-3.2-1b", "prompt": PROMPT, "max_tokens": 2}
        first = (await client.post("/v1/generate", json=body)).json()
        second = (await client.post("/v1/generate", json=body)).json()
        metrics = (await client.get("/metrics/summary")).json()["models"]
        models = (await client.get("/v1/models")).json()["models"]

    assert first["model"] == second["model"] == "economy"
    assert (first["usage"]["cached_tokens"], second["usage"]["cached_tokens"]) == (0, 10)
    assert second["cost"]["usd"] < first["cost"]["usd"]
    assert second["timing"]["total_ms"] < first["timing"]["total_ms"]
    assert metrics["economy"]["cached_tokens"] == 10
    assert set(metrics) == {"economy", "standard", "premium"}
    assert models == [
        {"id": "economy", "aliases": ["llama-3.2-1b"]},
        {"id": "standard", "aliases": ["llama-3.2-3b"]},
        {"id": "premium", "aliases": ["llama-3.1-8b"]},
    ]


async def test_shutdown_stops_the_scheduler_loops_and_cancels_what_is_in_flight() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock)
    worker = registry.get("premium")
    assert isinstance(worker, ContinuousBatchingWorker)

    async with app.router.lifespan_context(app):
        in_flight = asyncio.create_task(registry.submit(GenerateRequest("r1", "premium", PROMPT)))
        queued = asyncio.create_task(registry.submit(GenerateRequest("r2", "economy", PROMPT)))
        await clock.sleep_ms(1)  # both have been admitted and are being served
        assert not in_flight.done()
        assert not queued.done()

    await asyncio.gather(in_flight, queued, return_exceptions=True)
    assert in_flight.cancelled()
    assert queued.cancelled()
    with pytest.raises(RuntimeError, match="closed"):
        await registry.submit(GenerateRequest("r3", "premium", PROMPT))


async def test_default_app_serves_the_three_tiers_on_wall_time() -> None:
    app = create_default_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        models = (await client.get("/v1/models")).json()["models"]
        health = (await client.get("/healthz")).json()

    assert [m["id"] for m in models] == ["economy", "standard", "premium"]
    assert health["status"] == "ok"
    assert health["uptime_ms"] >= 0.0
