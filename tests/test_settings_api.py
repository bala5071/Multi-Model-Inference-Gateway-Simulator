"""Settings on the request path (docs/39): read with the key, on ``/v1/generate`` and on chat.

- A missing row is the defaults: today's values.
- A row that cannot be judged (it does not parse, fails validation, has a newer schema
  version, names a setting this code does not have, is not canonical) refuses that
  application's requests with 503 ``state_unavailable``, byte for byte the answer a store that
  cannot answer gives, and never serves them with a looser value.
- Each of S1 to S11 changes what a request gets; ``default_max_tokens`` is applied and enforced
  as the request's ``max_tokens``, and ``X-Max-Tokens-Applied`` says what was applied.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app
from gateway.applications import (
    ApplicationStore,
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
)
from gateway.audit import AuditLog, SqliteAuditStore
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import DEFAULT_ORGANIZATION_ID
from gateway.overload import OverloadPolicy, SloPolicy
from gateway.routing import load_plans
from gateway.settings import Scope, SettingsStore, SqliteSettingsStore
from gateway.settingsservice import SettingsService
from gateway.state import StateUnavailableError
from helpers import TEST_PEPPER, bearer

PLANS = load_plans()
PROMPT = "one two three four five six seven eight nine ten"
HEADER = "X-Max-Tokens-Applied"


@dataclass
class Settings:
    client: httpx.AsyncClient
    clock: SimClock
    key: str
    store: ApplicationStore
    settings: SettingsStore
    service: SettingsService
    corrupt: Callable[..., Awaitable[None]]

    async def generate(self, **body: Any) -> httpx.Response:
        body.setdefault("prompt", PROMPT)
        if "quality" not in body and body.get("model", "") is not None:
            body.setdefault("model", "economy")
        body = {k: v for k, v in body.items() if v is not None}
        return await self.client.post("/v1/generate", json=body, headers=bearer(self.key))

    async def chat(self, **body: Any) -> httpx.Response:
        body.setdefault("messages", [{"role": "user", "content": PROMPT}])
        if "quality" not in body and body.get("model", "") is not None:
            body.setdefault("model", "economy")
        body = {k: v for k, v in body.items() if v is not None}
        return await self.client.post("/v1/chat/completions", json=body, headers=bearer(self.key))

    async def set(self, scope: Scope = Scope.APPLICATION, **values: Any) -> None:
        scope_id = "shop" if scope is Scope.APPLICATION else DEFAULT_ORGANIZATION_ID
        row = await self.settings.current(scope, scope_id)
        await self.service.set(
            scope, scope_id, expected_version=row.version if row else 0, values=values
        )


@asynccontextmanager
async def _gateway(
    kind: str = "memory",
    *,
    plan: str = "enterprise",
    tmp_path: Path | None = None,
    overload: OverloadPolicy | None = None,
) -> AsyncIterator[Settings]:
    clock = SimClock()
    epoch = ClockEpoch(clock)
    registry = build_continuous_registry(clock, seed=7)
    application = application_from_template(
        "shop", "Shop", PLANS, plan, organization_id=DEFAULT_ORGANIZATION_ID, created_at_ms=0.0
    )
    connection = None
    if kind == "memory":
        memory = InMemoryApplicationStore()
        store: ApplicationStore = memory
        settings: SettingsStore = memory.settings
        audit = AuditLog(_ListAudit())  # type: ignore[arg-type]

        async def corrupt(scope: Scope, scope_id: str, **changes: Any) -> None:
            memory.settings.corrupt(scope, scope_id, **changes)

    else:
        assert tmp_path is not None
        connection = open_database(tmp_path / "gateway.sqlite3")
        MigrationRunner(connection, MIGRATIONS, epoch).migrate()
        store = SqliteApplicationStore(connection)
        settings = SqliteSettingsStore(connection)
        audit = AuditLog(SqliteAuditStore(connection))

        async def corrupt(scope: Scope, scope_id: str, **changes: Any) -> None:
            assert connection is not None
            for column, value in changes.items():
                connection.execute(
                    f"UPDATE settings_current SET {column} = ?"
                    " WHERE scope_type = ? AND scope_id = ?",
                    (value, scope.value, scope_id),
                )

    await store.add_application(application)
    keys = ApiKeyService(store, TEST_PEPPER, epoch)
    key = (await keys.issue("shop", "test")).key
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, PLANS),
        overload=overload,
        epoch=epoch,
    )
    service = SettingsService(settings, store, audit, epoch, app.state.settings_platform)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Settings(client, clock, key, store, settings, service, corrupt)
    await registry.aclose()
    if connection is not None:
        connection.close()


class _ListAudit:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def insert(self, event: Any, at_ms: float) -> None:
        self.rows.append((at_ms, event))

    async def entries(self) -> list[Any]:
        return []


@pytest.fixture(params=["memory", "sqlite"])
async def gw(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Settings]:
    async with _gateway(request.param, tmp_path=tmp_path) as found:
        yield found


# --- missing, unreadable, invalid: the table of docs/settings-and-alerts-plan.md, section 3 -------


async def test_a_missing_row_is_the_platform_defaults(gw: Settings) -> None:
    generated = await gw.generate(model="premium", max_tokens=4)
    chatted = await gw.chat(model="premium", max_tokens=4)
    assert generated.status_code == 200, generated.text
    assert chatted.status_code == 200, generated.text
    assert generated.json()["usage"]["completion_tokens"] <= 4
    assert generated.headers[HEADER] == "4"
    assert chatted.headers[HEADER] == "4"
    # No limit anywhere and none asked for: nothing is applied, as before settings existed.
    plain = await gw.generate(model="economy")
    assert plain.status_code == 200
    assert plain.headers[HEADER] == "none"


async def _reference_503(gw: Settings, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """What a request gets when its key lookup cannot be answered: the reference bytes."""

    async def down(*args: Any, **kwargs: Any) -> None:
        raise StateUnavailableError("down")

    with monkeypatch.context() as patch:
        patch.setattr(type(gw.store), "lookup_key", down)
        refused = await gw.generate()
    assert refused.status_code == 503
    return refused.content


UNREADABLE = [
    pytest.param({"document": '{"max_tokens":5'}, id="json"),
    pytest.param({"document": '{"max_tokens":0}'}, id="invalid-value"),
    pytest.param({"document": '{"future_setting":1}'}, id="unknown-setting"),
    pytest.param({"schema_version": 2}, id="newer-schema"),
    pytest.param({"document": '{ "max_tokens": 5 }'}, id="not-canonical"),
]


@pytest.mark.parametrize("scope", [Scope.APPLICATION, Scope.ORGANIZATION])
@pytest.mark.parametrize("changes", UNREADABLE)
async def test_a_row_that_cannot_be_judged_refuses_every_request_with_the_stores_503(
    gw: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scope: Scope,
    changes: dict[str, Any],
) -> None:
    reference = await _reference_503(gw, monkeypatch)
    await gw.set(scope, max_tokens=50)
    scope_id = "shop" if scope is Scope.APPLICATION else DEFAULT_ORGANIZATION_ID
    await gw.corrupt(scope, scope_id, **changes)
    with caplog.at_level(logging.WARNING, logger="gateway"):
        generated = await gw.generate()
        chatted = await gw.chat()
        usage = await gw.client.get("/v1/applications/me/usage", headers=bearer(gw.key))
    for refused in (generated, chatted, usage):
        assert refused.status_code == 503
        assert refused.content == reference
        assert HEADER not in refused.headers
    # The log says which scope, version and check; never the document.
    logged = [r.getMessage() for r in caplog.records if "settings" in r.getMessage()]
    assert logged, caplog.records
    for message in logged:
        assert "{" not in message
        assert "max_tokens" not in message


async def test_a_corrupt_cap_never_lets_a_request_through_without_it(gw: Settings) -> None:
    """An organization that allows only economy: corrupt its row, and premium is not served
    (the defaults would allow it); nothing is served at all until the row is fixed."""
    await gw.set(Scope.ORGANIZATION, allowed_tiers=["economy"])
    assert (await gw.generate(model="premium")).status_code == 403
    await gw.corrupt(Scope.ORGANIZATION, DEFAULT_ORGANIZATION_ID, document='{"allowed_tiers":[]}')
    for model in ("premium", "economy"):
        assert (await gw.generate(model=model)).status_code == 503
        assert (await gw.chat(model=model)).status_code == 503
    await gw.service.set(
        Scope.ORGANIZATION,
        DEFAULT_ORGANIZATION_ID,
        expected_version=1,
        values={"allowed_tiers": ["economy"]},
        replace=True,
    )
    assert (await gw.generate(model="premium")).status_code == 403
    assert (await gw.generate(model="economy")).status_code == 200


# --- each setting applied (S1 to S11) -------------------------------------------------------------


async def test_allowed_tiers_and_an_organization_cap_on_them(gw: Settings) -> None:
    await gw.set(allowed_tiers=["economy", "standard"])
    assert (await gw.generate(model="premium")).status_code == 403
    assert (await gw.chat(model="premium")).status_code == 403
    best = await gw.generate(model=None, quality="best")
    assert best.headers["X-Model-Used"] == "standard"
    await gw.set(Scope.ORGANIZATION, allowed_tiers=["economy"])
    assert (await gw.generate(model=None, quality="best")).headers["X-Model-Used"] == "economy"
    usage = (await gw.client.get("/v1/applications/me/usage", headers=bearer(gw.key))).json()
    assert usage["allowed_models"] == ["economy"]


async def test_no_allowed_tier_left_refuses_every_model_and_every_quality(gw: Settings) -> None:
    await gw.set(allowed_tiers=["premium"])
    await gw.set(Scope.ORGANIZATION, allowed_tiers=["economy"])
    assert (await gw.generate(model="economy")).status_code == 403
    assert (await gw.generate(model=None, quality="cheapest")).status_code == 403
    assert (await gw.chat(model=None, quality="cheapest")).status_code == 403


async def test_a_model_alias_resolves_to_its_tier(gw: Settings) -> None:
    await gw.set(model_aliases={"fast": "economy", "smart": "premium"})
    fast = await gw.generate(model="fast")
    assert fast.status_code == 200
    assert fast.headers["X-Model-Used"] == "economy"
    chatted = await gw.chat(model="smart")
    assert chatted.status_code == 200
    assert chatted.headers["X-Model-Used"] == "premium"
    # Not in force once its tier is not allowed: an unknown model, as before.
    await gw.set(allowed_tiers=["economy"])
    assert (await gw.generate(model="smart")).status_code == 404


async def test_default_quality_serves_a_request_that_names_neither(gw: Settings) -> None:
    assert (await gw.generate(model=None)).status_code == 422
    assert (await gw.chat(model=None)).status_code == 422
    await gw.set(default_quality="cheapest")
    served = await gw.generate(model=None)
    assert served.status_code == 200
    assert served.headers["X-Model-Used"] == "economy"
    chatted = await gw.chat(model=None)
    assert chatted.status_code == 200
    assert chatted.headers["X-Model-Used"] == "economy"
    # Naming both is still refused.
    assert (await gw.generate(model="economy", quality="best")).status_code == 422


async def test_allow_downgrade_default_applies_when_a_request_does_not_say() -> None:
    policy = OverloadPolicy(
        request_timeout_ms=200.0,
        slo=SloPolicy(p99_ms={"premium": 100.0}, min_samples=2, min_hold_ms=0.0),
    )
    async with _gateway(overload=policy) as gw:
        for _ in range(2):
            assert (await gw.generate(model="premium", max_tokens=50)).status_code == 504
        stayed = await gw.generate(model="premium", max_tokens=2)
        assert "X-Downgraded-From" not in stayed.headers
        await gw.set(allow_downgrade_default=True)
        moved = await gw.generate(model="premium", max_tokens=2)
        assert moved.headers["X-Downgraded-From"] == "premium"
        asked_not = await gw.generate(model="premium", max_tokens=2, allow_downgrade=False)
        assert "X-Downgraded-From" not in asked_not.headers


async def test_requests_per_minute_is_enforced_at_the_lowest_level(gw: Settings) -> None:
    await gw.set(requests_per_minute=2)
    assert [(await gw.generate()).status_code for _ in range(3)] == [200, 200, 429]
    assert (await gw.chat()).status_code == 429


async def test_the_monthly_budget_is_the_lowest_level(gw: Settings) -> None:
    await gw.set(Scope.ORGANIZATION, monthly_token_budget=0)
    refused = await gw.generate()
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "budget_exhausted"
    assert (await gw.chat()).status_code == 429


async def test_max_tokens_is_the_largest_a_request_may_ask(gw: Settings) -> None:
    await gw.set(max_tokens=8)
    assert (await gw.generate(max_tokens=9)).status_code == 422
    assert (await gw.chat(max_tokens=9)).status_code == 422
    assert (await gw.generate(max_tokens=8)).status_code == 200


async def test_the_generate_timeout_is_the_lowest_level() -> None:
    async with _gateway(overload=OverloadPolicy(request_timeout_ms=120_000.0)) as gw:
        assert (await gw.generate(model="premium", max_tokens=400)).status_code == 200
        await gw.set(generate_timeout_ms=1_000)
        timed_out = await gw.generate(model="premium", max_tokens=400)
        assert timed_out.status_code == 504
        assert timed_out.headers[HEADER] == "400"


# --- default_max_tokens: applied and enforced, and the header -------------------------------------


async def test_default_max_tokens_is_applied_when_a_request_names_none(gw: Settings) -> None:
    await gw.set(default_max_tokens=3)
    for answer in (await gw.generate(model="premium"), await gw.chat(model="premium")):
        assert answer.status_code == 200
        assert answer.headers[HEADER] == "3"
    generated = await gw.generate(model="premium")
    assert generated.json()["usage"]["completion_tokens"] <= 3
    chatted = await gw.chat(model="premium")
    assert chatted.json()["usage"]["completion_tokens"] <= 3
    assert chatted.json()["choices"][0]["finish_reason"] == "length"
    # The request's own value wins, within max_tokens.
    own = await gw.generate(model="premium", max_tokens=6)
    assert own.headers[HEADER] == "6"
    assert own.json()["usage"]["completion_tokens"] <= 6


async def test_default_max_tokens_follows_max_tokens_when_unset(gw: Settings) -> None:
    await gw.set(max_tokens=5)
    answer = await gw.generate(model="premium")
    assert answer.headers[HEADER] == "5"
    assert answer.json()["usage"]["completion_tokens"] <= 5


async def test_the_header_is_on_streamed_answers_and_not_on_early_refusals(gw: Settings) -> None:
    await gw.set(default_max_tokens=2)
    streamed = await gw.generate(stream=True)
    assert streamed.status_code == 200
    assert streamed.headers[HEADER] == "2"
    chat_streamed = await gw.chat(stream=True)
    assert chat_streamed.status_code == 200
    assert chat_streamed.headers[HEADER] == "2"
    unknown = await gw.generate(model="nope")
    assert unknown.status_code == 404
    assert HEADER not in unknown.headers
    bad = await gw.client.post(
        "/v1/generate", json={"model": "economy", "prompt": PROMPT}, headers=bearer("gwk_x")
    )
    assert bad.status_code == 401
    assert HEADER not in bad.headers
    await gw.set(max_tokens=2)
    too_many = await gw.generate(max_tokens=3)
    assert too_many.status_code == 422
    assert HEADER not in too_many.headers


async def test_without_applications_the_header_says_what_the_request_asked() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        asked = await client.post(
            "/v1/generate", json={"model": "economy", "prompt": PROMPT, "max_tokens": 3}
        )
        plain = await client.post("/v1/generate", json={"model": "economy", "prompt": PROMPT})
    await registry.aclose()
    assert asked.headers[HEADER] == "3"
    assert plain.headers[HEADER] == "none"


# --- the leak scan --------------------------------------------------------------------------------

PLANTED_PROMPT = "plantedpromptcanary4b7e"
PLANTED_ALIAS = "planteddoccanary9c2d"


async def test_no_prompt_key_or_stored_document_leaks_into_rows_logs_or_answers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requests carry a planted prompt and the key; settings are written, changed, reverted, and
    then the application's row is corrupted with a planted alias in it. Every settings row and
    version, every audit row, every log record and every answer is scanned. The controls: the
    scan finds the planted alias in the stored row (where it belongs) and the prompt in what
    was sent, so a scan that found nothing would have found it."""
    answers: list[httpx.Response] = []
    sent: list[bytes] = []
    with caplog.at_level(logging.DEBUG):
        async with _gateway("sqlite", tmp_path=tmp_path) as gw:
            gw.client.event_hooks["request"].append(lambda request: _record(sent, request))
            await gw.set(max_tokens=40, model_aliases={"fast": "economy"})
            await gw.set(default_max_tokens=3)
            await gw.service.revert(Scope.APPLICATION, "shop", expected_version=2)
            await gw.set(Scope.ORGANIZATION, requests_per_minute=100)
            messages = [{"role": "user", "content": PLANTED_PROMPT}]
            answers.append(await gw.generate(prompt=PLANTED_PROMPT, model="fast"))
            answers.append(await gw.chat(messages=messages))
            await gw.corrupt(
                Scope.APPLICATION,
                "shop",
                document='{"max_tokens":0,"model_aliases":{"' + PLANTED_ALIAS + '":"economy"}}',
            )
            answers.append(await gw.generate(prompt=PLANTED_PROMPT))
            answers.append(await gw.chat(messages=[{"role": "user", "content": PLANTED_PROMPT}]))
            assert isinstance(gw.store, SqliteApplicationStore)
            connection = gw.store._connection
            rows = repr(
                connection.execute("SELECT * FROM settings_current").fetchall()
                + connection.execute("SELECT * FROM settings_versions").fetchall()
            )
            audit = repr(connection.execute("SELECT * FROM audit_log").fetchall())
            key = gw.key
    logged = "\n".join(record.getMessage() for record in caplog.records)
    answered = "\n".join(r.text + repr(sorted(r.headers.items())) for r in answers)
    assert [r.status_code for r in answers] == [200, 200, 503, 503]
    # The controls: found where they are meant to be.
    assert PLANTED_ALIAS in rows
    assert any(PLANTED_PROMPT.encode() in body for body in sent)
    assert any(key.encode() in body for body in sent)
    # And nowhere else.
    for planted in (PLANTED_PROMPT, key):
        assert planted not in rows
        assert planted not in audit
        assert planted not in logged
    assert PLANTED_PROMPT not in answered.replace("[simulated output", "")
    assert key not in answered
    assert PLANTED_ALIAS not in logged
    assert PLANTED_ALIAS not in answered
    assert PLANTED_ALIAS not in audit
    assert "settings unreadable: application shop version 3, check validation" in logged


async def _record(sent: list[bytes], request: httpx.Request) -> None:
    sent.append(await request.aread() + repr(request.headers.raw).encode())
