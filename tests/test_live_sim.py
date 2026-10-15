"""Simulated live mode and the control endpoints: the replay is a function of its seed."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from gateway.app import create_app, create_default_app, create_simulated_live_app
from gateway.clock import PacedSimClock, SimClock
from gateway.continuous import build_continuous_registry
from gateway.loadcontrol import UnknownPresetError
from gateway.routing import load_plans


class Waits:
    def __init__(self) -> None:
        self.total_s = 0.0

    async def __call__(self, seconds: float) -> None:
        self.total_s += seconds


@dataclasses.dataclass
class Replay:
    events: list[dict[str, Any]]
    snapshot: dict[str, Any]
    control: dict[str, Any]
    real_seconds: float


@asynccontextmanager
async def _running(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """The app with its startup and shutdown run, as a server would run them."""
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gateway") as client,
    ):
        yield client


async def _replay(seed: int, *, speed: float = 20.0, seconds: float = 150.0) -> Replay:
    """Simulated live mode's overload preset for ``seconds`` of simulated time."""
    waits = Waits()
    app = create_simulated_live_app(
        seed=seed, speed=speed, preset="overload", wait=waits, dashboard_dir=None
    )
    async with _running(app) as client:
        await app.state.clock.sleep_ms(seconds * 1000.0)
        events = [dataclasses.asdict(e) for e in app.state.monitoring.events.since(0).events]
        snapshot = (await client.get("/v1/monitor/snapshot?history_s=900&events=1000")).json()
        control = (await client.get("/v1/control")).json()
    return Replay(events, snapshot, control, waits.total_s)


# --- determinism ---------------------------------------------------------------------------


async def test_the_same_seed_gives_the_same_event_sequence() -> None:
    first = await _replay(7)
    again = await _replay(7)

    # The replay has something to compare: premium is pushed past its queue limit.
    assert len(first.events) > 20
    assert first.events[0]["seq"] == 1
    assert "queue_full" in {e["type"] for e in first.events}
    # Every event: its number, its simulated time, its model, tenant and details.
    assert first.events == again.events
    # And everything else the dashboard is shown: every series point, total and tenant.
    assert first.snapshot == again.snapshot
    assert first.control == again.control


async def test_a_different_seed_gives_a_different_sequence() -> None:
    first = await _replay(7, seconds=60.0)
    other = await _replay(8, seconds=60.0)

    assert first.events != other.events
    assert first.snapshot["models"] != other.snapshot["models"]


async def test_the_speed_changes_how_long_it_takes_and_nothing_else() -> None:
    slow = await _replay(7, speed=1.0, seconds=60.0)
    fast = await _replay(7, speed=20.0, seconds=60.0)

    assert slow.events == fast.events
    assert slow.snapshot == fast.snapshot
    # Sixty simulated seconds: sixty real ones at 1x, three at 20x (none of them really
    # waited for here).
    assert slow.real_seconds == pytest.approx(60.0, abs=0.01)
    assert fast.real_seconds == pytest.approx(3.0, abs=0.01)


async def test_replay_times_are_simulated_times() -> None:
    replay = await _replay(7, seconds=60.0)

    assert replay.snapshot["at_ms"] == 60_000.0
    status = replay.control["load"]["status"]
    assert (status["running"], status["preset"], status["seed"], status["started_ms"]) == (
        True,
        "overload",
        7,
        0.0,
    )
    # About 6 requests a second for 60 seconds.
    assert 300 <= status["sent"] <= 420
    assert status["sent"] == status["served"] + status["refused"] + status["in_progress"]
    arrived = sum(m["traffic"]["arrived_total"] for m in replay.snapshot["models"].values())
    # A request refused for its tenant's budget never reaches a model; the rest all do.
    assert arrived <= status["sent"]
    assert replay.control["simulation"] == {"active": True, "speed": 20.0, "speeds": [1, 5, 20]}
    assert replay.control["seed"] == 7


# --- the control endpoints -----------------------------------------------------------------


async def test_control_is_off_by_default_and_says_so() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    async with _running(create_app(registry, clock, plans=load_plans())) as client:
        status = (await client.get("/v1/control")).json()
        start = await client.post("/v1/control/load/start", json={"preset": "steady"})
        stop = await client.post("/v1/control/load/stop")
        speed = await client.post("/v1/control/speed", json={"speed": 5})

    assert status == {
        "seed": 0,
        "load": {"available": False, "presets": None, "status": None},
        "simulation": {"active": False, "speed": None, "speeds": None},
    }
    for response in (start, stop):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "load_control_disabled"
    assert (speed.status_code, speed.json()["error"]["code"]) == (409, "not_simulated")


async def test_load_can_be_started_and_stopped_through_the_api() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock, plans=load_plans(), load_control=True, seed=42)
    async with _running(app) as client:
        idle = (await client.get("/v1/control")).json()
        started = await client.post("/v1/control/load/start", json={"preset": "steady"})
        await clock.sleep_ms(5000.0)
        running = (await client.get("/v1/control")).json()
        snapshot = (await client.get("/v1/monitor/snapshot")).json()
        with_seed = await client.post("/v1/control/load/start", json={"preset": "burst", "seed": 9})
        unknown = await client.post("/v1/control/load/start", json={"preset": "tsunami"})
        stopped = await client.post("/v1/control/load/stop")
        sent = stopped.json()["load"]["status"]["sent"]
        await clock.sleep_ms(5000.0)
        later = (await client.get("/v1/control")).json()

    assert idle["load"]["available"] is True
    assert [p["name"] for p in idle["load"]["presets"]] == [
        "steady",
        "burst",
        "overload",
        "long-prompts",
    ]
    assert idle["load"]["status"]["running"] is False
    assert idle["simulation"]["active"] is False
    # No seed given: the gateway's own is used, and reported.
    assert started.status_code == 200
    assert (started.json()["load"]["status"]["preset"], started.json()["seed"]) == ("steady", 42)
    assert started.json()["load"]["status"]["seed"] == 42
    status = running["load"]["status"]
    # About 8 requests a second for 5 seconds, through the ordinary request path: the
    # models saw them and the tenants were charged.
    assert 20 <= status["sent"] <= 70
    assert status["served"] > 0
    assert sum(m["traffic"]["arrived_total"] for m in snapshot["models"].values()) > 0
    assert sum(t["requests"] for t in snapshot["tenants"]["tenants"].values()) == status["served"]
    assert with_seed.json()["load"]["status"]["seed"] == 9
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_preset")
    assert unknown.json()["error"]["valid_presets"] == [
        "steady",
        "burst",
        "overload",
        "long-prompts",
    ]
    assert stopped.json()["load"]["status"]["running"] is False
    assert later["load"]["status"]["sent"] == sent


async def test_speed_can_be_set_to_the_offered_values_only() -> None:
    app = create_simulated_live_app(seed=1, speed=1.0, wait=Waits(), dashboard_dir=None)
    clock = app.state.clock
    assert isinstance(clock, PacedSimClock)
    async with _running(app) as client:
        faster = await client.post("/v1/control/speed", json={"speed": 20})
        speed_after = clock.speed
        odd = await client.post("/v1/control/speed", json={"speed": 1000})
        missing = await client.post("/v1/control/speed", json={})

    assert faster.status_code == 200
    assert faster.json()["simulation"] == {"active": True, "speed": 20.0, "speeds": [1, 5, 20]}
    assert speed_after == 20.0
    assert (odd.status_code, odd.json()["error"]["code"]) == (422, "invalid_speed")
    assert odd.json()["error"]["valid_speeds"] == [1, 5, 20]
    assert clock.speed == 20.0
    assert missing.status_code == 422


async def test_shutdown_stops_the_load() -> None:
    app = create_simulated_live_app(seed=1, wait=Waits(), dashboard_dir=None)
    async with _running(app):
        await app.state.clock.sleep_ms(2000.0)
        assert app.state.load.status().running is True
        assert app.state.load.status().preset == "steady"

    assert app.state.load.status().running is False
    assert app.state.load.status().in_progress == 0


# --- the server option ---------------------------------------------------------------------


async def test_default_app_enters_simulated_live_mode_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_SIMULATED_LIVE", "1")
    monkeypatch.setenv("GATEWAY_SIM_SPEED", "5")
    monkeypatch.setenv("GATEWAY_SIM_PRESET", "burst")
    monkeypatch.setenv("GATEWAY_SEED", "21")

    app = create_default_app()

    assert isinstance(app.state.clock, PacedSimClock)
    assert app.state.clock.speed == 5.0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        control = (await client.get("/v1/control")).json()
    assert control["seed"] == 21
    assert control["simulation"]["active"] is True
    assert control["load"]["available"] is True


def test_simulated_live_mode_rejects_settings_it_cannot_honour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_SIMULATED_LIVE", "1")
    monkeypatch.setenv("GATEWAY_SIM_SPEED", "3")
    with pytest.raises(ValueError, match="GATEWAY_SIM_SPEED must be 1, 5 or 20"):
        create_default_app()

    monkeypatch.setenv("GATEWAY_SIM_SPEED", "1")
    monkeypatch.setenv("GATEWAY_SIM_PRESET", "tsunami")
    with pytest.raises(UnknownPresetError):
        create_default_app()

    monkeypatch.setenv("GATEWAY_SIM_PRESET", "steady")
    monkeypatch.setenv("GATEWAY_BACKEND", "llamacpp")
    with pytest.raises(ValueError, match="cannot be combined with GATEWAY_BACKEND=llamacpp"):
        create_default_app()


async def test_default_app_offers_load_control_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def available() -> bool:
        app = create_default_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            control = (await client.get("/v1/control")).json()
        assert control["simulation"]["active"] is False
        return bool(control["load"]["available"])

    assert await available() is False
    monkeypatch.setenv("GATEWAY_LOAD_CONTROL", "1")
    assert await available() is True
