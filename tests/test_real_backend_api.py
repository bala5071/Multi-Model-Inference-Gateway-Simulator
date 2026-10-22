"""The whole gateway in front of (mock) real model servers, and the backend config."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app, create_default_app
from gateway.backend_config import (
    BackendConfig,
    BackendConfigError,
    LlamaServer,
    build_llamacpp_registry,
    load_backend_config,
    parse_backend_config,
)
from gateway.clock import SimClock
from gateway.cost_model import estimate_cost
from gateway.passthrough import PassthroughWorker
from gateway.profiles import PREMIUM, Tier
from helpers import application_access, bearer, default_server_key
from mock_llama import MockLlamaServer

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = BackendConfig(
    kind="llamacpp",
    servers={
        Tier.ECONOMY: LlamaServer("http://127.0.0.1:8081"),
        Tier.STANDARD: LlamaServer("http://127.0.0.1:8082"),
        Tier.PREMIUM: LlamaServer("http://127.0.0.1:8083"),
    },
)


@asynccontextmanager
async def _gateway(
    server: MockLlamaServer,
    clock: SimClock,
    config: BackendConfig = CONFIG,
    application: str = "acme",
) -> AsyncIterator[httpx.AsyncClient]:
    registry = build_llamacpp_registry(config, clock, client=server.client())
    access, keys = application_access(clock)
    app = create_app(registry, clock, applications=access)
    transport = httpx.ASGITransport(app=app)
    # Every request is for one application (acme unless the test says otherwise).
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", headers=bearer(keys[application])
    ) as client:
        yield client
    await registry.aclose()


def _events(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in response.text.split("\n\n"):
        if block:
            name_line, data_line = block.split("\n")
            events.append((name_line.removeprefix("event: "), json.loads(data_line[6:])))
    return events


def _body(**fields: Any) -> dict[str, Any]:
    return {"prompt": "Say hello to the world", **fields}


# --- serving -------------------------------------------------------------------------------


async def test_gateway_returns_the_models_text_usage_and_measured_timing() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    async with _gateway(server, clock) as client:
        response = await client.post("/v1/generate", json=_body(model="premium", max_tokens=16))
        usage = (await client.get("/v1/applications/me/usage")).json()
        metrics = (await client.get("/metrics/summary")).json()["models"]["premium"]

    base = estimate_cost(PREMIUM, new_prefill_tokens=7, output_tokens=3, batch_size=1).cost_usd
    assert response.status_code == 200
    assert response.headers["X-Model-Used"] == "premium"
    assert response.json() == {
        "id": "req-1",
        "text": "Hello world!",
        "model": "premium",
        "usage": {
            "prompt_tokens": 7,
            "cached_tokens": 0,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
        # Measured from the stream: first piece after 50 ms, last after 70 ms.
        "timing": {"queue_ms": 0.0, "ttft_ms": 50.0, "total_ms": 70.0},
        "cost": {"usd": base * 0.8},  # acme's enterprise multiplier
    }
    assert server.bodies[0]["max_tokens"] == 16
    assert usage["total"]["requests"] == 1
    assert metrics["ttft_ms"] == {"p50": 50.0, "p99": 50.0}
    assert metrics["itl_ms"] == {"p50": 10.0, "p99": 10.0}


async def test_each_tier_is_sent_to_its_own_server() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    async with _gateway(server, clock) as client:
        for choice in (
            {"model": "economy"},
            {"model": "standard"},
            {"model": "premium"},
            {"model": "llama-3.2-3b"},
            {"quality": "best"},
            {"quality": "cheapest"},
        ):
            assert (await client.post("/v1/generate", json=_body(**choice))).status_code == 200

    assert [r.url.port for r in server.requests] == [8081, 8082, 8083, 8082, 8083, 8081]


async def test_plan_limits_still_apply_with_real_backends() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    async with _gateway(server, clock, application="initech") as client:
        response = await client.post("/v1/generate", json=_body(model="premium"))
    assert response.status_code == 403
    assert server.requests == []  # the model server was never contacted


async def test_stream_carries_the_models_real_text_and_arrival_times() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    async with _gateway(server, clock) as client:
        response = await client.post("/v1/generate", json=_body(model="economy", stream=True))

    events = _events(response)
    assert events[:3] == [
        ("token", {"index": 0, "token": "Hello", "t_ms": 50.0}),
        ("token", {"index": 1, "token": " world", "t_ms": 60.0}),
        ("token", {"index": 2, "token": "!", "t_ms": 70.0}),
    ]
    name, done = events[3]
    assert name == "done"
    assert done["text"] == "Hello world!"
    assert done["timing"] == {"queue_ms": 0.0, "ttft_ms": 50.0, "total_ms": 70.0}


async def test_model_name_from_the_config_is_sent_to_the_server() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    ollama = BackendConfig(
        kind="llamacpp",
        endpoint="chat",
        servers={
            Tier.ECONOMY: LlamaServer("http://127.0.0.1:11434", "llama3.2:1b"),
            Tier.STANDARD: LlamaServer("http://127.0.0.1:11434", "llama3.2:3b"),
            Tier.PREMIUM: LlamaServer("http://127.0.0.1:11434", "llama3.1:8b"),
        },
    )
    async with _gateway(server, clock, ollama) as client:
        await client.post("/v1/generate", json=_body(model="premium"))
        await client.post("/v1/generate", json=_body(model="economy"))

    assert [b["model"] for b in server.bodies] == ["llama3.1:8b", "llama3.2:1b"]
    assert all(r.url.path == "/v1/chat/completions" for r in server.requests)


# --- failures ------------------------------------------------------------------------------


async def test_server_down_is_a_502_with_a_clear_message() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, down={8083})
    async with _gateway(server, clock) as client:
        down = await client.post("/v1/generate", json=_body(model="premium"))
        streamed = await client.post("/v1/generate", json=_body(model="premium", stream=True))
        other = await client.post("/v1/generate", json=_body(model="economy"))
        usage = (await client.get("/v1/applications/me/usage")).json()
        metrics = (await client.get("/metrics/summary")).json()["models"]

    assert down.status_code == 502
    assert down.json() == {
        "error": {
            "code": "backend_unavailable",
            "message": "Model backend 'premium' at http://127.0.0.1:8083: could not connect. "
            "Is the model server running? See docs/12-real-models.md.",
            "backend": "premium",
            "url": "http://127.0.0.1:8083",
        }
    }
    # The same for a streaming request, since nothing had been sent yet.
    assert streamed.status_code == 502
    assert streamed.headers["content-type"] == "application/json"
    assert streamed.json() == down.json()
    # One tier being down does not affect another, and failures are not charged or counted.
    assert other.status_code == 200
    assert list(usage["by_model"]) == ["economy"]
    assert metrics["premium"]["requests"] == 0


async def test_error_from_the_model_server_is_a_502() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, status={8082: 503})
    async with _gateway(server, clock) as client:
        response = await client.post("/v1/generate", json=_body(model="standard"))

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "backend_error"
    assert "answered HTTP 503" in error["message"]
    assert error["backend"] == "standard"


async def test_connection_lost_mid_stream_ends_the_stream_with_an_error_event() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, drop_after=2)
    async with _gateway(server, clock) as client:
        streamed = await client.post("/v1/generate", json=_body(model="economy", stream=True))
        plain = await client.post("/v1/generate", json=_body(model="economy"))
        usage = (await client.get("/v1/applications/me/usage")).json()

    events = _events(streamed)
    assert [name for name, _ in events] == ["token", "token", "error"]
    assert events[-1][1]["code"] == "backend_error"
    assert "failed mid-response" in events[-1][1]["message"]
    # Without streaming the same failure is a plain 502.
    assert plain.status_code == 502
    assert usage["total"]["requests"] == 0


# --- config --------------------------------------------------------------------------------


def test_packaged_config_selects_the_simulator_and_defines_the_three_servers() -> None:
    config = load_backend_config()
    assert config.kind == "simulated"
    assert config.endpoint == "completions"
    assert {tier: server.url for tier, server in config.servers.items()} == {
        Tier.ECONOMY: "http://127.0.0.1:8081",
        Tier.STANDARD: "http://127.0.0.1:8082",
        Tier.PREMIUM: "http://127.0.0.1:8083",
    }
    assert load_backend_config(kind_override="llamacpp").kind == "llamacpp"


def test_ollama_example_config_is_valid() -> None:
    config = load_backend_config(REPO_ROOT / "examples" / "backends.ollama.yaml")
    assert config.kind == "llamacpp"
    assert {s.url for s in config.servers.values()} == {"http://127.0.0.1:11434"}
    assert [config.servers[t].model for t in Tier] == [
        "llama3.2:1b-instruct-q4_K_M",
        "llama3.2:3b",
        "llama3.1:8b",
    ]


def _document() -> dict[str, Any]:
    return {
        "backend": "llamacpp",
        "llamacpp": {
            "endpoint": "chat",
            "max_in_flight": 4,
            "servers": {
                "economy": {"url": "http://h:1"},
                "standard": {"url": "http://h:2", "model": "m"},
                "premium": {"url": "https://h:3"},
            },
        },
    }


def test_parse_backend_config_reads_a_valid_document() -> None:
    config = parse_backend_config(_document())
    assert (config.kind, config.endpoint, config.max_in_flight) == ("llamacpp", "chat", 4)
    assert config.servers[Tier.STANDARD] == LlamaServer("http://h:2", "m")
    assert parse_backend_config({}).kind == "simulated"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("backend",), "vllm", "must be 'simulated' or 'llamacpp'"),
        (("llamacpp",), ["x"], "'llamacpp' must be a mapping"),
        (("llamacpp", "endpoint"), "responses", "endpoint must be"),
        (("llamacpp", "max_in_flight"), 0, "positive integer"),
        (("llamacpp", "max_in_flight"), "many", "positive integer"),
        (("llamacpp", "servers"), ["x"], "must map each tier"),
        (("llamacpp", "servers", "economy"), "http://h:1", "needs a 'url'"),
        (("llamacpp", "servers", "economy", "url"), "h:1", r"must start with http\(s\)://"),
        (("llamacpp", "servers", "standard", "model"), "", "non-empty string"),
        (("llamacpp", "servers", "ultra"), {"url": "http://h:9"}, "'ultra' is not a tier"),
    ],
)
def test_parse_backend_config_rejects_bad_documents(
    path: tuple[str, ...], value: Any, message: str
) -> None:
    document = _document()
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(BackendConfigError, match=message):
        parse_backend_config(document)


def test_llamacpp_needs_a_server_for_every_tier() -> None:
    document = _document()
    del document["llamacpp"]["servers"]["premium"]
    with pytest.raises(BackendConfigError, match="no server is configured for: premium"):
        parse_backend_config(document)
    # The same file is fine while the simulator is selected.
    assert parse_backend_config(document, kind_override="simulated").kind == "simulated"


def test_backends_file_must_be_valid_yaml_and_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "backends.yaml"
    path.write_text("backend: [unclosed\n", encoding="utf-8")
    with pytest.raises(BackendConfigError, match="not valid YAML"):
        load_backend_config(path)
    with pytest.raises(BackendConfigError, match="must be a mapping"):
        parse_backend_config(["backend"])


def test_registry_for_real_backends_uses_passthrough_workers_with_the_usual_names() -> None:
    clock = SimClock()
    config = BackendConfig(kind="llamacpp", max_in_flight=5, servers=CONFIG.servers)
    registry = build_llamacpp_registry(config, clock, client=MockLlamaServer(clock).client())

    assert registry.names == ("economy", "standard", "premium")
    assert registry.resolve("llama-3.1-8b") == "premium"
    assert all(isinstance(registry.get(name), PassthroughWorker) for name in registry.names)


# --- the default app, by config ------------------------------------------------------------


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def test_default_app_switches_backend_by_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point every tier at a port nothing listens on: with the llamacpp backend selected
    # the request must reach for it and fail with 502; with the simulator it must not.
    port = _unused_port()
    servers = "".join(f"    {t.value}: {{url: 'http://127.0.0.1:{port}'}}\n" for t in Tier)
    path = tmp_path / "backends.yaml"
    path.write_text(f"backend: simulated\nllamacpp:\n  servers:\n{servers}", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_BACKENDS", str(path))
    body = _body(model="economy", max_tokens=1)
    key = default_server_key("acme")

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_default_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            return await client.post("/v1/generate", json=body, headers=bearer(key))

    simulated = await post()
    monkeypatch.setenv("GATEWAY_BACKEND", "llamacpp")
    real = await post()

    assert simulated.status_code == 200
    assert simulated.json()["text"] == "[simulated output, 1 tokens]"
    assert real.status_code == 502
    assert real.json()["error"]["code"] == "backend_unavailable"
    assert real.json()["error"]["url"] == f"http://127.0.0.1:{port}"
