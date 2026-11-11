"""Serving the built dashboard, and the snapshot fixtures the frontend's tests are typed by."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


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


# The users of tests/test_capabilities.py's world whose capabilities answers the frontend's
# tests read (docs/50): named by user, so the frontend holds no role name.
CAPABILITY_USERS = ("root", "alice", "vera", "nina", "olive", "cleo", "bob")


async def _capabilities() -> dict[str, Any]:
    """``GET /v1/auth/capabilities`` for each of those users, in their own organization."""
    from test_capabilities import gateway

    answers: dict[str, Any] = {}
    async with gateway() as g:
        for username in CAPABILITY_USERS:
            answers[f"capabilities-{username}.json"] = await g.capabilities(await g.login(username))
    return answers


async def _console_reads() -> dict[str, Any]:
    """The Applications list's reads (docs/51), as alice (north's org_admin) gets them: the
    list with each mode, the figures over a whole window and over one that began at a restart,
    the open incident counts, the plan templates, and north-shop's keys (never a key itself)."""
    from gateway.modes import Mode
    from test_console_reads import console, seed_incidents

    answers: dict[str, Any] = {}
    async with console() as c:
        alice = await c.login("alice")
        # Just after the start: the window reaches before this process's rollups began.
        c.at_minute(5)
        c.record("north-shop", 4, status=200, total_ms=12.0)
        c.evaluated_after(5)
        answers["metrics-alice-restart.json"] = (await c.get(alice, "/north/metrics")).json()
        # Later: a whole window, with enough requests to judge north-shop and too few for blog.
        c.at_minute(100)
        c.record("north-shop", 14, status=200, total_ms=12.0)
        c.record("north-shop", 6, status=200, total_ms=180.0)
        c.record("north-shop", 5, status=502, code="upstream_unavailable")
        c.record("north-blog", 3, status=200, total_ms=40.0)
        c.evaluated_after(120)
        answers["metrics-alice.json"] = (await c.get(alice, "/north/metrics")).json()
        await c.modes.set_organization_mode("north", Mode.LOCAL_WITH_HOSTED_FALLBACK, 0.0)
        await c.modes.set_application_mode("north-shop", Mode.LOCAL_WITH_HOSTED_FALLBACK, 0.0)
        answers["applications-alice.json"] = (await c.get(alice, "/north/applications")).json()
        await seed_incidents(c.incidents)
        answers["incident-counts-alice.json"] = (
            await c.get(alice, "/north/incidents/counts")
        ).json()
        answers["plans-alice.json"] = (await c.get(alice, "/north/plans")).json()
        await c.step_up(alice)
        keys = "/north/applications/north-shop/keys"
        for label in ("first", "second"):
            assert (await c.post(alice, keys, {"label": label})).status_code == 201
        listed = (await c.get(alice, keys)).json()
        second = next(k["key_id"] for k in listed["keys"] if k["label"] == "second")
        assert (await c.post(alice, f"{keys}/{second}/revoke", {})).status_code == 200
        answers["keys-alice.json"] = (await c.get(alice, keys)).json()
    return answers


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
        **await _capabilities(),
        **await _console_reads(),
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


# --- the page holds no role name, and asks only what the gateway answers (docs/50) -----------

FRONTEND_SRC = FIXTURES.parent.parent
ROLE_NAMES = re.compile(r"\b(platform_admin|org_admin|app_admin|viewer)\b")


def role_names_in(text: str) -> list[str]:
    return ROLE_NAMES.findall(text)


def test_the_frontend_names_no_role() -> None:
    """Who may do what is the gateway's answer (GET /v1/auth/capabilities); the frontend shows the
    session's roles as the gateway names them and never spells one itself. The fixtures, which the
    gateway produced, are the exception."""
    found = []
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".css", ".html", ".json"}:
            continue
        if FIXTURES in path.parents:
            continue
        for name in role_names_in(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(FRONTEND_SRC)}: {name}")
    assert found == []
    # The control: the same scan finds a role name where one is written.
    assert role_names_in('const allowed = role === "org_admin";') == ["org_admin"]
    assert role_names_in("a viewer may read") == ["viewer"]


def test_the_frontend_asks_only_actions_the_gateway_answers_at_their_level() -> None:
    """Every action in the page's navigation map is one of NAVIGATION_ACTIONS, at the level the
    capabilities answer files it under."""
    from gateway.permissions import NAVIGATION_ACTIONS

    source = (FRONTEND_SRC / "shell" / "navigation.ts").read_text(encoding="utf-8")
    asked = re.findall(r'level: "([a-z]+)", action: "([a-z_.]+)"', source)
    assert len(asked) >= 9
    answered = {
        action: level.value for level, actions in NAVIGATION_ACTIONS.items() for action in actions
    }
    for level, action in asked:
        assert answered.get(action) == level, (level, action)
