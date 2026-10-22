"""Serving the built dashboard, and the snapshot fixtures the frontend's tests are typed by."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from gateway.app import create_app, create_simulated_live_app
from gateway.backend_config import BackendConfig, LlamaServer, build_llamacpp_registry
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.overload import OverloadPolicy, SloPolicy
from gateway.profiles import Tier
from helpers import TEST_PROFILES, application_access, application_headers
from mock_llama import MockLlamaServer

FIXTURES = Path(__file__).resolve().parent.parent / "frontend" / "src" / "test" / "fixtures"
UPDATE_ENV = "GATEWAY_UPDATE_FIXTURES"


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


# --- /dashboard ----------------------------------------------------------------------------


async def test_dashboard_serves_the_built_folder(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Gateway monitor</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    async with _client(create_app(registry, clock, dashboard_dir=tmp_path)) as client:
        index = await client.get("/dashboard/")
        asset = await client.get("/dashboard/assets/app.js")
        missing = await client.get("/dashboard/assets/nope.js")
        api = await client.get("/healthz")
    await registry.aclose()

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "Gateway monitor" in index.text
    assert (asset.status_code, asset.text) == (200, "console.log('hi')")
    assert missing.status_code == 404
    assert api.status_code == 200


async def test_dashboard_that_was_not_built_says_how_to_build_it(tmp_path: Path) -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    # No folder given, and a folder with no index.html in it.
    for app in (create_app(registry, clock), create_app(registry, clock, dashboard_dir=tmp_path)):
        async with _client(app) as client:
            for path in ("/dashboard", "/dashboard/", "/dashboard/assets/app.js"):
                response = await client.get(path)
                assert response.status_code == 404
                assert response.json()["error"]["code"] == "dashboard_not_built"
                assert "npm run build" in response.json()["error"]["message"]
    await registry.aclose()


# --- fixtures ------------------------------------------------------------------------------


async def _simulated_snapshot() -> dict[str, Any]:
    """Three simulated models with plans and SLOs, after a surge that degrades premium.

    A scripted scenario on the hand-made test profiles (fixtures, not data), so that what
    the frontend's tests check can be followed by hand.
    """
    clock = SimClock()
    registry = build_continuous_registry(
        clock, seed=7, max_queue_len={Tier.PREMIUM: 2}, profiles=TEST_PROFILES
    )
    slo = SloPolicy(
        p99_ms={"economy": 5000.0, "standard": 10_000.0, "premium": 2000.0},
        window_ms=10_000.0,
        min_samples=5,
        min_hold_ms=1000.0,
    )
    access, keys = application_access(clock)
    app = create_app(registry, clock, applications=access, overload=OverloadPolicy(slo=slo))
    counter = 0

    async with _client(app) as client:

        async def generate(**body: Any) -> httpx.Response:
            nonlocal counter
            counter += 1
            body.setdefault("prompt", " ".join(f"p{counter}w{i}" for i in range(10)))
            headers = application_headers(body, keys)
            return await client.post("/v1/generate", json=body, headers=headers)

        for second in range(3):
            await asyncio.gather(
                generate(application_id="acme", model="economy", max_tokens=8),
                generate(application_id="globex", model="standard", max_tokens=8),
                generate(application_id="initech", model="economy", max_tokens=4),
            )
            await clock.sleep_ms((second + 1) * 1000.0 - clock.now_ms())
        # Twelve premium requests at once: eight are seated, two wait, two are refused,
        # and the slowest take long enough to put premium over its SLO.
        await asyncio.gather(
            *(generate(application_id="acme", model="premium", max_tokens=60) for _ in range(12))
        )
        await generate(application_id="acme", model="premium", max_tokens=4, allow_downgrade=True)
        snapshot: dict[str, Any] = (
            await client.get("/v1/monitor/snapshot?history_s=900&events=50")
        ).json()
    await registry.aclose()
    return snapshot


async def _passthrough_snapshot() -> dict[str, Any]:
    """Three models behind pass-through workers: the scheduler groups are unavailable."""
    clock = SimClock()
    server = MockLlamaServer(clock)
    config = BackendConfig(
        kind="llamacpp",
        servers={tier: LlamaServer(f"http://127.0.0.1:808{i}") for i, tier in enumerate(Tier, 1)},
    )
    registry = build_llamacpp_registry(config, clock, client=server.client())
    async with _client(create_app(registry, clock)) as client:
        await client.post("/v1/generate", json={"model": "premium", "prompt": "Say hello"})
        snapshot: dict[str, Any] = (await client.get("/v1/monitor/snapshot?history_s=900")).json()
    await registry.aclose()
    return snapshot


async def _no_wait(_: float) -> None:
    return None


async def _simulated_control() -> dict[str, Any]:
    """GET /v1/control ten simulated seconds into simulated live mode."""
    app = create_simulated_live_app(seed=7, speed=5.0, wait=_no_wait, dashboard_dir=None)
    async with app.router.lifespan_context(app), _client(app) as client:
        await app.state.clock.sleep_ms(10_000.0)
        control: dict[str, Any] = (await client.get("/v1/control")).json()
    return control


async def _plain_control() -> dict[str, Any]:
    """GET /v1/control on a gateway with neither load control nor simulation."""
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    async with _client(create_app(registry, clock)) as client:
        control: dict[str, Any] = (await client.get("/v1/control")).json()
    await registry.aclose()
    return control


async def test_frontend_fixtures_are_what_the_gateway_produces() -> None:
    """The frontend's types and tests are checked against these files.

    If the snapshot's shape or values change, this fails until the fixtures are
    regenerated with ``GATEWAY_UPDATE_FIXTURES=1 uv run pytest tests/test_dashboard.py``,
    and the frontend's type check then shows what its code has to follow.
    """
    produced = {
        "snapshot-simulated.json": await _simulated_snapshot(),
        "snapshot-passthrough.json": await _passthrough_snapshot(),
        "control-simulated.json": await _simulated_control(),
        "control-off.json": await _plain_control(),
    }
    if os.environ.get(UPDATE_ENV) == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, snapshot in produced.items():
            (FIXTURES / name).write_text(
                json.dumps(snapshot, indent=2) + "\n", encoding="utf-8", newline="\n"
            )

    for name, snapshot in produced.items():
        assert json.loads((FIXTURES / name).read_text(encoding="utf-8")) == snapshot, name

    # The fixtures cover the states the dashboard has to draw.
    simulated = produced["snapshot-simulated.json"]
    assert simulated["models"]["premium"]["slo"]["degraded"] is True
    assert simulated["models"]["economy"]["slo"]["degraded"] is False
    assert {e["type"] for e in simulated["events"]["recent"]} == {
        "queue_full",
        "state_change",
        "downgrade",
    }
    assert simulated["applications"]["available"] is True
    assert produced["control-simulated.json"]["simulation"]["active"] is True
    assert produced["control-simulated.json"]["load"]["status"]["running"] is True
    assert produced["control-off.json"]["load"]["available"] is False
    passthrough = produced["snapshot-passthrough.json"]
    assert passthrough["applications"]["available"] is False
    for model in passthrough["models"].values():
        assert not any(model[group]["available"] for group in ("queue", "batch", "kv", "cache"))
        assert model["slo"]["available"] is False


async def test_stream_takes_the_last_event_id_as_a_query_parameter() -> None:
    clock = SimClock()
    registry = build_continuous_registry(
        clock, seed=7, max_queue_len={Tier.PREMIUM: 0}, profiles=TEST_PROFILES
    )
    async with _client(create_app(registry, clock)) as client:
        # The test profile seats eight and three refused: events 1, 2 and 3.
        await asyncio.gather(
            *(
                client.post(
                    "/v1/generate",
                    json={"model": "premium", "prompt": f"p{i} hello", "max_tokens": 2},
                )
                for i in range(11)
            )
        )

        async def event_ids(query: str, headers: dict[str, str] | None = None) -> list[str]:
            response = await client.get(f"/v1/monitor/stream?count=1{query}", headers=headers)
            assert response.status_code == 200
            return [
                line.removeprefix("id: ")
                for line in response.text.splitlines()
                if line.startswith("id: ")
            ]

        fresh = await event_ids("")
        everything = await event_ids("&last_event_id=0")
        after_two = await event_ids("&last_event_id=2")
        # The query wins over the header when both are given.
        both = await event_ids("&last_event_id=1", {"Last-Event-ID": "3"})
        invalid = await client.get("/v1/monitor/stream?count=1&last_event_id=-1")
    await registry.aclose()

    assert fresh == []
    assert everything == ["1", "2", "3"]
    assert after_two == ["3"]
    assert both == ["2", "3"]
    assert invalid.status_code == 422
