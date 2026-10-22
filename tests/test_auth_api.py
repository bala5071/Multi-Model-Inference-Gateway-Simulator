"""API keys through the HTTP API: who may call, what a bad key gets, isolation between
applications, per-application limits, the CLI, and a scan proving no key or pepper value
leaks anywhere the gateway writes. See docs/20-applications-api-keys.md."""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.apikeys import PEPPER_ENV, ApiKeyService, ApplicationAccess, PepperError
from gateway.app import create_app, create_default_app, create_simulated_live_app
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.monitoring import Monitor
from gateway.routing import DemoApplication, Plan, PlanLimits, PlansConfig, load_plans
from helpers import TEST_PEPPER, UNKNOWN_KEY, bearer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

PROMPT = "one two three four five six seven eight nine ten"
HOUR_MS = 3_600_000.0


@dataclass
class Gateway:
    client: httpx.AsyncClient
    clock: SimClock
    keys: ApiKeyService
    app: Any
    connection: sqlite3.Connection
    db_path: Path

    async def generate(self, key: str | None, **body: Any) -> httpx.Response:
        body.setdefault("model", "economy")
        body.setdefault("prompt", PROMPT)
        body.setdefault("max_tokens", 4)
        headers = bearer(key) if key is not None else {}
        return await self.client.post("/v1/generate", json=body, headers=headers)

    async def usage(self, key: str) -> httpx.Response:
        return await self.client.get("/v1/applications/me/usage", headers=bearer(key))


@asynccontextmanager
async def _gateway(tmp_path: Path, templates: PlansConfig | None = None) -> AsyncIterator[Gateway]:
    """The gateway with applications and keys in a SQLite database, as the server has."""
    templates = templates if templates is not None else load_plans()
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=1.7e12)
    db_path = tmp_path / "gateway.db"
    connection = open_database(db_path)
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
    keys = ApiKeyService(store, TEST_PEPPER, epoch, random_bytes=random.Random(3).randbytes)
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock, applications=ApplicationAccess(keys, templates))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, keys, app, connection, db_path)
    await registry.aclose()
    connection.close()


# --- who may call --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization", [None, "", "Bearer", "Bearer   ", "Basic dXNlcjpwYXNz", "Token abc"]
)
async def test_a_request_without_a_bearer_key_is_401_api_key_required(
    tmp_path: Path, authorization: str | None
) -> None:
    async with _gateway(tmp_path) as gw:
        headers = {} if authorization is None else {"Authorization": authorization}
        body = {"model": "economy", "prompt": PROMPT}
        response = await gw.client.post("/v1/generate", json=body, headers=headers)
        usage = await gw.client.get("/v1/applications/me/usage", headers=headers)
    for answer in (response, usage):
        assert answer.status_code == 401
        assert answer.json()["error"]["code"] == "api_key_required"
        assert answer.headers["WWW-Authenticate"] == "Bearer"
    assert gw.clock.now_ms() == 0.0


async def test_the_key_is_checked_before_the_body(tmp_path: Path) -> None:
    async with _gateway(tmp_path) as gw:
        response = await gw.client.post("/v1/generate", json={"nonsense": True})
    assert response.status_code == 401  # not 422: nothing is said to a caller without a key


async def test_key_lifecycle_through_the_api(tmp_path: Path) -> None:
    async with _gateway(tmp_path) as gw:
        old = gw.keys.issue("acme", "backend")
        assert (await gw.generate(old.key)).status_code == 200

        new = gw.keys.rotate(old.record.key_id, grace_ms=HOUR_MS)
        # During the grace period both keys work, for the same application.
        assert (await gw.generate(old.key)).status_code == 200
        assert (await gw.generate(new.key)).status_code == 200
        await gw.clock.sleep_ms(HOUR_MS)
        assert (await gw.generate(old.key)).status_code == 401
        assert (await gw.generate(new.key)).status_code == 200

        gw.keys.revoke(new.record.key_id)
        assert (await gw.generate(new.key)).status_code == 401
        assert (await gw.usage(old.key)).status_code == 401

        gw.keys.flush_last_used()
        records = gw.keys.store.list_keys("acme")
        # Two keys, both used, both now refused; the four served requests were acme's.
        assert [(r.label, r.revoked) for r in records] == [("backend", False), ("backend", True)]
        assert all(r.last_used_at_ms is not None for r in records)
        third = gw.keys.issue("acme", "check").key
        assert (await gw.usage(third)).json()["total"]["requests"] == 4


async def test_every_bad_key_gets_the_identical_answer(tmp_path: Path) -> None:
    async with _gateway(tmp_path) as gw:
        valid = gw.keys.issue("acme", "valid")
        revoked = gw.keys.issue("acme", "revoked")
        gw.keys.revoke(revoked.record.key_id)
        expired = gw.keys.issue("acme", "expired", expires_at_ms=gw.keys._epoch.epoch_ms())
        rotated = gw.keys.issue("acme", "rotated")
        gw.keys.rotate(rotated.record.key_id, grace_ms=0.0)
        candidates = {
            "unknown": UNKNOWN_KEY,
            "malformed": "not-a-key",
            "wrong secret": valid.key[:-2] + ("AA" if not valid.key.endswith("AA") else "BB"),
            "revoked": revoked.key,
            "expired": expired.key,
            "rotated out": rotated.key,
        }
        answers = {name: await gw.generate(key) for name, key in candidates.items()}
        usage = {name: await gw.usage(key) for name, key in candidates.items()}

    def shape(response: httpx.Response) -> tuple[int, bytes, list[tuple[str, str]]]:
        return response.status_code, response.content, sorted(response.headers.items())

    first = shape(answers["unknown"])
    assert first[0] == 401
    assert json.loads(first[1]) == {
        "error": {"code": "invalid_api_key", "message": "The API key is not valid."}
    }
    for name, response in [*answers.items(), *usage.items()]:
        assert shape(response) == first, name


# --- isolation between applications ---------------------------------------------------------


async def test_an_application_sees_only_its_own_usage(tmp_path: Path) -> None:
    async with _gateway(tmp_path) as gw:
        acme = gw.keys.issue("acme", "a").key
        globex = gw.keys.issue("globex", "g").key
        for _ in range(3):
            await gw.generate(acme)
        await gw.generate(globex, model="standard")
        acme_usage = (await gw.usage(acme)).json()
        globex_usage = (await gw.usage(globex)).json()
        # There is no way to name another application: the old per-tenant URL is gone.
        old_url = await gw.client.get("/v1/tenants/globex/usage", headers=bearer(acme))

    assert (acme_usage["application_id"], acme_usage["total"]["requests"]) == ("acme", 3)
    assert (globex_usage["application_id"], globex_usage["total"]["requests"]) == ("globex", 1)
    assert list(globex_usage["by_model"]) == ["standard"]
    assert old_url.status_code == 404


async def test_budgets_are_per_application(tmp_path: Path) -> None:
    templates = PlansConfig(
        plans={"tiny": Plan("tiny", ("economy",), 1.0, 20)},
        applications={"a": DemoApplication("A", "tiny"), "b": DemoApplication("B", "tiny")},
    )
    async with _gateway(tmp_path, templates) as gw:
        a = gw.keys.issue("a", "a").key
        b = gw.keys.issue("b", "b").key
        # Prompts that differ from their first word, so no prefix is reused (and left unbilled).
        statuses = [(await gw.generate(a, prompt=f"{i} {PROMPT}")).status_code for i in range(3)]
        other = (await gw.generate(b)).status_code
        used = (await gw.usage(a)).json()["used_tokens"]
    # Each bills its 11 prompt tokens and more: two pass the check (0, then 11+), the third not.
    assert statuses == [200, 200, 429]
    assert used >= 20
    assert other == 200


async def test_requests_per_minute_limit_answers_429_with_retry_after(tmp_path: Path) -> None:
    templates = replace(load_plans(), limits={"pro": PlanLimits(requests_per_minute=2)})
    async with _gateway(tmp_path, templates) as gw:
        globex = gw.keys.issue("globex", "g").key
        acme = gw.keys.issue("acme", "a").key
        first = [(await gw.generate(globex)).status_code for _ in range(2)]
        limited = await gw.generate(globex)
        elsewhere = await gw.generate(acme)  # enterprise has no limit
        await gw.clock.sleep_ms(60_000.0)
        later = await gw.generate(globex)
        snapshot = (await gw.client.get("/v1/monitor/snapshot?history_s=120")).json()
        prometheus = (await gw.client.get("/metrics")).text

    # The refusal is a rejection of the application, with its own reason, in the snapshot
    # and in Prometheus; it never reached a model.
    rejections = snapshot["applications"]["applications"]["globex"]["rejections_total"]
    assert rejections == {"budget_exhausted": 0, "queue_full": 0, "rate_limited": 1}
    assert (
        'gateway_application_rejections_total{application="globex",reason="rate_limited"} 1'
        in prometheus
    )
    assert sum(m["traffic"]["arrived_total"] for m in snapshot["models"].values()) == 4
    assert first == [200, 200]
    assert limited.status_code == 429
    error = limited.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["limit_per_minute"] == 2
    assert limited.headers["Retry-After"] == str(error["retry_after_s"])
    assert 1 <= error["retry_after_s"] <= 60
    assert elsewhere.status_code == 200
    assert later.status_code == 200


async def test_max_tokens_ceiling_refuses_more_and_caps_requests_that_ask_for_none(
    tmp_path: Path,
) -> None:
    templates = replace(load_plans(), limits={"free": PlanLimits(max_tokens=3)})
    async with _gateway(tmp_path, templates) as gw:
        initech = gw.keys.issue("initech", "i").key
        too_many = await gw.generate(initech, max_tokens=4)
        unspecified = await gw.generate(initech, max_tokens=None)
        within = await gw.generate(initech, max_tokens=2)
        usage = (await gw.usage(initech)).json()

    assert too_many.status_code == 422
    [detail] = too_many.json()["error"]["details"]
    assert detail == {"field": "max_tokens", "message": "At most 3 on this application's plan."}
    assert unspecified.status_code == 200
    assert unspecified.json()["usage"]["completion_tokens"] <= 3
    assert within.json()["usage"]["completion_tokens"] <= 2
    assert (usage["max_tokens"], usage["total"]["requests"]) == (3, 2)


# --- the default server -----------------------------------------------------------------------


def test_the_default_server_refuses_to_start_without_the_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PEPPER_ENV)
    with pytest.raises(PepperError, match=PEPPER_ENV):
        create_default_app()
    monkeypatch.setenv(PEPPER_ENV, "too-short")
    with pytest.raises(PepperError) as excinfo:
        create_default_app()
    assert "too-short" not in str(excinfo.value)


async def test_a_key_issued_by_the_cli_works_against_the_default_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert manage(["seed-demo"]) == 0
    assert manage(["create-key", "--application", "globex", "--label", "cli"]) == 0
    key = capsys.readouterr().out.strip()
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        ok = await client.post(
            "/v1/generate",
            json={"quality": "best", "prompt": PROMPT, "max_tokens": 2},
            headers=bearer(key),
        )
        usage = await client.get("/v1/applications/me/usage", headers=bearer(key))
    assert ok.status_code == 200
    assert ok.headers["X-Model-Used"] == "standard"
    assert usage.json()["application_id"] == "globex"


# --- the CLI ----------------------------------------------------------------------------------


def test_the_cli_prints_a_new_key_once_and_stores_only_its_hmac(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    environ = {"GATEWAY_DATABASE": str(tmp_path / "cli.db"), PEPPER_ENV: TEST_PEPPER.decode()}
    assert manage(["seed-demo"], environ) == 0
    capsys.readouterr()
    assert manage(["create-key", "--application", "acme", "--label", "x"], environ) == 0
    issued = capsys.readouterr()
    key = issued.out.strip()
    assert issued.out == key + "\n"  # alone on stdout, once
    assert key not in issued.err
    assert "cannot be shown again" in issued.err

    assert manage(["list-keys", "--application", "acme"], environ) == 0
    listed = capsys.readouterr().out
    key_id = key[4:20]
    assert key_id in listed
    assert key not in listed

    assert manage(["rotate-key", "--key-id", key_id, "--grace-hours", "1"], environ) == 0
    rotated = capsys.readouterr()
    new_key = rotated.out.strip()
    assert new_key != key
    assert manage(["revoke-key", "--key-id", key_id], environ) == 0
    assert "revoked" in capsys.readouterr().err

    stored = b"".join(p.read_bytes() for p in tmp_path.glob("cli.db*"))
    for value in (key, new_key, TEST_PEPPER.decode()):
        assert value.encode() not in stored
        assert all(value not in r.getMessage() for r in caplog.records)


def test_the_cli_refuses_to_issue_without_the_pepper(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    environ = {"GATEWAY_DATABASE": str(tmp_path / "cli.db")}
    assert manage(["seed-demo"], environ) == 0
    with pytest.raises(PepperError):
        manage(["create-key", "--application", "acme", "--label", "x"], environ)
    assert "gwk_" not in capsys.readouterr().out


# --- the Playground ---------------------------------------------------------------------------


async def test_the_playground_sends_as_an_application_in_simulated_live_mode() -> None:
    async def never(_: float) -> None:
        return None

    app = create_simulated_live_app(wait=never, dashboard_dir=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        ok = await client.post(
            "/v1/playground/generate",
            json={"application_id": "acme", "quality": "best", "prompt": PROMPT, "max_tokens": 2},
        )
        unknown = await client.post(
            "/v1/playground/generate",
            json={"application_id": "nobody", "model": "economy", "prompt": PROMPT},
        )
        control = (await client.get("/v1/control")).json()
    assert ok.status_code == 200
    assert ok.headers["X-Model-Used"] == "premium"
    assert unknown.status_code == 401
    assert control["playground"] == {"available": True}
    assert "gwk_" not in ok.text + unknown.text + json.dumps(control)


async def test_the_playground_is_not_offered_outside_simulated_live_mode(tmp_path: Path) -> None:
    async with _gateway(tmp_path) as gw:
        response = await gw.client.post(
            "/v1/playground/generate",
            json={"application_id": "acme", "model": "economy", "prompt": PROMPT},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "playground_unavailable"


# --- no key or pepper anywhere ----------------------------------------------------------------


def _places(
    gw: Gateway, caplog: pytest.LogCaptureFixture, answers: list[bytes]
) -> dict[str, bytes]:
    monitoring: Monitor = gw.app.state.monitoring
    tables = []
    for (table,) in gw.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        tables.append(repr(gw.connection.execute(f"SELECT * FROM {table}").fetchall()))
    return {
        "database files": b"".join(p.read_bytes() for p in gw.db_path.parent.glob("gateway.db*")),
        "database rows": "\n".join(tables).encode(),
        "log records": "\n".join(
            f"{r.name} {r.getMessage()} {r.args!r} {r.__dict__!r}" for r in caplog.records
        ).encode(),
        "events": json.dumps(
            [dataclasses.asdict(e) for e in monitoring.events.since(0).events]
        ).encode(),
        "responses and errors": b"\n".join(answers),
    }


def _leaks(places: dict[str, bytes], secrets: dict[str, str]) -> list[str]:
    return [
        f"{name} found in {place}"
        for place, haystack in places.items()
        for name, value in secrets.items()
        if value.encode() in haystack
    ]


async def test_no_key_or_pepper_appears_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    async with _gateway(tmp_path) as gw:
        good = gw.keys.issue("acme", "good")
        revoked = gw.keys.issue("acme", "revoked")
        gw.keys.revoke(revoked.record.key_id)
        rotated = gw.keys.rotate(good.record.key_id, grace_ms=HOUR_MS)
        answers = []
        for key in (good.key, revoked.key, rotated.key, UNKNOWN_KEY):
            for body in ({}, {"model": "premium-x"}, {"stream": True}, {"tenant_id": "acme"}):
                answers.append((await gw.generate(key, **body)).content)
            answers.append((await gw.usage(key)).content)
        for path in ("/v1/monitor/snapshot?history_s=900&events=1000", "/metrics", "/"):
            answers.append((await gw.client.get(path)).content)
        gw.keys.flush_last_used()
        places = _places(gw, caplog, answers)
    output = capfd.readouterr()
    places["test output"] = (output.out + output.err).encode()

    secrets = {
        "good key": good.key,
        "revoked key": revoked.key,
        "rotated key": rotated.key,
        "pepper": TEST_PEPPER.decode(),
    }
    # The scenario did run: requests were served and the keys were used.
    assert any(b'"model":"economy"' in a for a in answers)
    assert _leaks(places, secrets) == []


def test_the_scan_finds_a_planted_key(tmp_path: Path) -> None:
    key = "gwk_0123456789abcdef_" + "Z" * 43
    secrets = {"planted key": key, "pepper": TEST_PEPPER.decode()}
    assert _leaks({"clean": b"nothing here"}, secrets) == []
    for place, planted in {
        "log record": f"INFO gateway authenticated {key}".encode(),
        "database row": repr([(1, key)]).encode(),
        "event": json.dumps({"data": {"key": key}}).encode(),
        "test output": b"pepper=" + TEST_PEPPER,
    }.items():
        assert _leaks({place: planted}, secrets), place
