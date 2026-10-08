"""Server-sent-event streaming on /v1/generate, on simulated time."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.app import create_app
from gateway.backends import SimulatedBackend
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, StepRecord, build_continuous_registry
from gateway.cost_model import estimate_cost
from gateway.overload import OverloadPolicy
from gateway.profiles import PREMIUM
from gateway.registry import ModelRegistry
from gateway.routing import PlansConfig, load_plans
from helpers import EXACT

# Ten words: 10 prompt tokens, so 5 ms of prefill on the EXACT profile.
PROMPT = "one two three four five six seven eight nine ten"


@dataclass
class Gateway:
    client: httpx.AsyncClient
    clock: SimClock
    app: Any
    steps: list[StepRecord] = field(default_factory=list)

    async def generate(self, **body: Any) -> httpx.Response:
        return await self.client.post("/v1/generate", json=body)


@asynccontextmanager
async def _gateway(
    *,
    overload: OverloadPolicy | None = None,
    max_queue_len: int | None = None,
) -> AsyncIterator[Gateway]:
    """One model, "standard", on the EXACT test profile; 30 output tokens unless capped."""
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
            max_queue_len=max_queue_len,
        ),
    )
    app = create_app(registry, clock, overload=overload)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, app, steps)
    await registry.aclose()


def _events(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    """Parse a server-sent-event body into (event name, decoded data) pairs."""
    events = []
    for block in response.text.split("\n\n"):
        if not block:
            continue
        name_line, data_line = block.split("\n")
        assert name_line.startswith("event: ")
        assert data_line.startswith("data: ")
        events.append((name_line.removeprefix("event: "), json.loads(data_line[6:])))
    return events


# --- the happy path ------------------------------------------------------------------------


async def test_stream_emits_one_event_per_token_as_decode_steps_happen() -> None:
    async with _gateway() as gateway:
        response = await gateway.generate(
            model="standard", prompt=PROMPT, max_tokens=3, stream=True
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Model-Used"] == "standard"
    assert "X-Cost-USD" not in response.headers  # not known until the stream ends
    # Prefill ends at 5 ms; the three decode steps end at 9, 13 and 17 ms.
    assert _events(response) == [
        ("token", {"index": 0, "token": "tok0", "t_ms": 9.0}),
        ("token", {"index": 1, "token": "tok1", "t_ms": 13.0}),
        ("token", {"index": 2, "token": "tok2", "t_ms": 17.0}),
        (
            "done",
            {
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
            },
        ),
    ]


async def test_final_event_equals_the_non_streaming_response() -> None:
    body = {"model": "standard", "prompt": PROMPT, "max_tokens": 7}
    async with _gateway() as gateway:
        plain = await gateway.generate(**body)
    async with _gateway() as gateway:
        streamed = await gateway.generate(**body, stream=True)

    events = _events(streamed)
    assert events[-1] == ("done", plain.json())
    assert [name for name, _ in events[:-1]] == ["token"] * 7


async def test_token_events_are_ordered_and_match_the_timing_summary() -> None:
    async with _gateway() as gateway:
        response = await gateway.generate(model="standard", prompt=PROMPT, stream=True)

    *tokens, (last_name, done) = _events(response)
    assert last_name == "done"
    assert [data["index"] for _, data in tokens] == list(range(30))
    assert [data["token"] for _, data in tokens] == [f"tok{i}" for i in range(30)]
    times = [data["t_ms"] for _, data in tokens]
    assert times == sorted(set(times))
    assert done["usage"]["completion_tokens"] == len(tokens)
    assert times[0] == done["timing"]["ttft_ms"]
    assert times[-1] == done["timing"]["total_ms"]
    # Alone in the batch, every gap is one 4 ms decode step.
    assert {later - earlier for earlier, later in itertools.pairwise(times)} == {4.0}


async def test_stream_defaults_to_off() -> None:
    async with _gateway() as gateway:
        default = await gateway.generate(model="standard", prompt=PROMPT, max_tokens=1)
        explicit = await gateway.generate(
            model="standard", prompt=PROMPT, max_tokens=1, stream=False
        )
        invalid = await gateway.generate(model="standard", prompt=PROMPT, stream="yes please")

    assert default.headers["content-type"] == "application/json"
    assert explicit.headers["content-type"] == "application/json"
    assert "X-Cost-USD" in default.headers
    assert invalid.status_code == 422
    assert [d["field"] for d in invalid.json()["error"]["details"]] == ["stream"]


async def test_concurrent_streams_share_decode_steps() -> None:
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]
    async with _gateway() as gateway:
        responses = await asyncio.gather(
            *(
                gateway.generate(model="standard", prompt=p, max_tokens=3, stream=True)
                for p in prompts
            )
        )
        steps = list(gateway.steps)

    assert [s.batch_size for s in steps] == [3, 3, 3]
    for response in responses:
        times = [data["t_ms"] for name, data in _events(response) if name == "token"]
        # Three prefills of 5 ms, then steps of 4.5 ms for the batch of three.
        assert times == [19.5, 24.0, 28.5]


# --- metrics -------------------------------------------------------------------------------


async def test_inter_token_latency_is_reported_per_model() -> None:
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]
    async with _gateway() as gateway:
        before = (await gateway.client.get("/metrics/summary")).json()["models"]["standard"]
        # One token: no gap to measure yet.
        await gateway.generate(model="standard", prompt=PROMPT, max_tokens=1)
        single = (await gateway.client.get("/metrics/summary")).json()["models"]["standard"]
        # A batch of three: gaps of 4.5 ms. Streamed or not, the gaps are recorded.
        await asyncio.gather(
            gateway.generate(model="standard", prompt=prompts[0], max_tokens=3, stream=True),
            gateway.generate(model="standard", prompt=prompts[1], max_tokens=3),
            gateway.generate(model="standard", prompt=prompts[2], max_tokens=3),
        )
        # And one alone: gaps of 4 ms.
        await gateway.generate(model="standard", prompt=PROMPT + " more", max_tokens=6)
        after = (await gateway.client.get("/metrics/summary")).json()["models"]["standard"]

    assert before["itl_ms"] is None
    assert single["itl_ms"] is None
    # Eleven gaps in all: six of 4.5 ms and five of 4 ms.
    assert after["itl_ms"] == {"p50": 4.5, "p99": 4.5}
    assert after["requests"] == 5


# --- errors --------------------------------------------------------------------------------


async def test_errors_before_the_first_token_are_plain_json_responses() -> None:
    async with _gateway(overload=OverloadPolicy(request_timeout_ms=3.0)) as gateway:
        unknown = await gateway.generate(model="gpt-4", prompt=PROMPT, stream=True)
        invalid = await gateway.generate(model="standard", stream=True)
        # Prefill alone takes 5 ms, so the 3 ms timeout fires before any token.
        timed_out = await gateway.generate(model="standard", prompt=PROMPT, stream=True)

    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_model")
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "invalid_request")
    assert (timed_out.status_code, timed_out.json()["error"]["code"]) == (504, "request_timeout")
    for response in (unknown, invalid, timed_out):
        assert response.headers["content-type"] == "application/json"


async def test_full_queue_is_a_429_for_streams_too() -> None:
    async with _gateway(max_queue_len=0) as gateway:
        responses = await asyncio.gather(
            *(
                gateway.generate(
                    model="standard", prompt=f"p{i} {PROMPT}", max_tokens=2, stream=True
                )
                for i in range(5)
            )
        )

    # EXACT seats four at once and the queue limit is zero.
    assert [r.status_code for r in responses] == [200, 200, 200, 200, 429]
    rejected = responses[-1]
    assert rejected.json()["error"]["code"] == "queue_full"
    assert rejected.headers["Retry-After"] == "1"
    assert all(_events(r)[-1][0] == "done" for r in responses[:4])


async def test_timeout_after_the_stream_has_started_ends_it_with_an_error_event() -> None:
    async with _gateway(overload=OverloadPolicy(request_timeout_ms=14.0)) as gateway:
        response = await gateway.generate(model="standard", prompt=PROMPT, stream=True)
        timed_out_at = gateway.clock.now_ms()
        metrics = (await gateway.client.get("/metrics/summary")).json()["models"]["standard"]
        following = await gateway.generate(model="standard", prompt="next " + PROMPT, max_tokens=1)

    # The status was already sent as 200; the failure arrives in the stream.
    assert response.status_code == 200
    assert timed_out_at == 14.0
    assert _events(response) == [
        ("token", {"index": 0, "token": "tok0", "t_ms": 9.0}),
        ("token", {"index": 1, "token": "tok1", "t_ms": 13.0}),
        (
            "error",
            {
                "code": "request_timeout",
                "message": "Model 'standard' did not finish the request within 14 ms. "
                "It was cancelled and has not been charged.",
                "model": "standard",
                "timeout_ms": 14.0,
            },
        ),
    ]
    assert metrics["requests"] == 0
    # The cancelled stream left the batch at the next step boundary (17 ms).
    assert following.json()["timing"]["queue_ms"] == 3.0


# --- with plans ----------------------------------------------------------------------------


async def test_streamed_request_is_routed_billed_and_charged_like_any_other() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    plans: PlansConfig = load_plans()
    transport = httpx.ASGITransport(app=create_app(registry, clock, plans=plans))
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.post(
            "/v1/generate",
            json={
                "tenant_id": "acme",
                "quality": "best",
                "prompt": PROMPT,
                "max_tokens": 4,
                "stream": True,
            },
        )
        forbidden = await client.post(
            "/v1/generate",
            json={"tenant_id": "initech", "model": "premium", "prompt": PROMPT, "stream": True},
        )
        usage = (await client.get("/v1/tenants/acme/usage")).json()
    await registry.aclose()

    *tokens, (name, done) = _events(response)
    base = estimate_cost(PREMIUM, new_prefill_tokens=10, output_tokens=4, batch_size=1).cost_usd
    assert response.headers["X-Model-Used"] == "premium"
    assert (name, done["model"], len(tokens)) == ("done", "premium", 4)
    assert done["cost"]["usd"] == base * 0.8
    assert usage["total"]["requests"] == 1
    assert usage["total"]["billed_cost_usd"] == base * 0.8
    assert forbidden.status_code == 403
    assert forbidden.headers["content-type"] == "application/json"


# --- client disconnect ---------------------------------------------------------------------


async def test_client_disconnecting_mid_stream_cancels_the_request() -> None:
    async with _gateway() as gateway:
        body = json.dumps({"model": "standard", "prompt": PROMPT, "stream": True}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/generate",
            "raw_path": b"/v1/generate",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"gateway"), (b"content-type", b"application/json")],
            "client": ("test-client", 1),
            "server": ("gateway", 80),
        }
        disconnected = asyncio.Event()
        request_sent = False
        chunks: list[bytes] = []

        async def receive() -> MutableMapping[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                if len(chunks) == 2:  # the client hangs up after the second token
                    disconnected.set()

        await gateway.app(scope, receive, send)
        await gateway.clock.sleep_ms(1000.0)
        steps = list(gateway.steps)
        after = await gateway.generate(model="standard", prompt="next " + PROMPT, max_tokens=1)

    assert b'"index": 1' in chunks[1]
    # The request had 30 tokens to produce. It was stopped within a step of the hang-up
    # (which came at 13 ms) instead of running for 30 steps to 125 ms.
    assert 2 <= len(steps) <= 3
    assert len(chunks) < 5
    # And its slot was free again: the next request did not wait.
    assert after.json()["timing"]["queue_ms"] == 0.0
