"""Shared budgets without a database (docs/37-budgets-on-postgres.md): every way a request ends,
and whether it settles or releases its reservation; the bound it reserves; the settings that let
shared mode start, or refuse it; the address rule for stores off this machine; and Valkey's
clock measured against Postgres's, with both clocks under the test's control.

The budgets here are a recording stand-in for ``PostgresBudgets``: what the request path asks of
them is the subject. ``tests/test_budgets_postgres.py`` has the real ones. Test values, not
secrets and not data.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gateway.adapters import OpenAICompatibleAdapter
from gateway.app import RESERVATION_MARGIN_MS, create_app, create_default_app
from gateway.backend_config import BackendConfigError, TierBudget, parse_backend_config
from gateway.backends import Backend
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.events import EventBus
from gateway.ledger import (
    ApplicationUsage,
    BudgetExceededError,
    CostLedger,
    Expired,
    ModelUsage,
    Reservation,
    run_sweep,
)
from gateway.overload import OverloadPolicy
from gateway.profiles import Tier
from gateway.schema import RequestResult
from gateway.state import (
    COUNTERS_ENV,
    POSTGRES_URL_ENV,
    PRIVATE_NETWORK_ENV,
    STATE_ENV,
    InsecureStoreAddressError,
    SharedStateRefusedError,
    StateUnavailableError,
    check_state_setting,
    check_store_address,
)
from gateway.valkey import URL_ENV as VALKEY_URL_SETTING
from gateway.valkey import ValkeyCounters
from helpers import TEST_PEPPER, TEST_PROFILES, application_access, bearer
from mock_providers import Reply, ScriptedProvider, openai_text
from valkey_helpers import ManualEpoch, ScriptedClient

PROMPT = "one two three four five six seven eight nine ten"
# Test settings for every tier, not any model's real numbers.
BOUNDS = {t.value: TierBudget(context_tokens=4096, reserve_completion_tokens=256) for t in Tier}
TIMEOUT_MS = 30_000.0


@dataclass
class RecordingBudgets:
    """``SharedBudgets`` that admit everything (or refuse, when told) and record each call."""

    refuse: BudgetExceededError | None = None
    healthy: bool = True
    retention_ms: float | None = None
    reserved: list[tuple[int, int, float]] = field(default_factory=list)  # id, tokens, hold
    ends: list[tuple[str, int]] = field(default_factory=list)  # settle or release, id
    expired: list[list[Expired]] = field(default_factory=list)  # what each sweep returns
    sweeps: int = 0

    async def reserve(
        self, application_id: str, budget_tokens: int, tokens: int, *, hold_ms: float
    ) -> Reservation:
        if self.refuse is not None:
            raise self.refuse
        reservation_id = len(self.reserved) + 1
        self.reserved.append((reservation_id, tokens, hold_ms))
        return Reservation(application_id, tokens, reservation_id)

    async def settle(
        self,
        reservation: Reservation,
        result: RequestResult,
        *,
        model: str,
        price_multiplier: float,
        source: Any = None,
    ) -> float:
        assert reservation.id is not None
        self.ends.append(("settle", reservation.id))
        return result.cost_usd * price_multiplier

    async def release(self, reservation: Reservation) -> None:
        assert reservation.id is not None
        self.ends.append(("release", reservation.id))

    async def usage(self, application_id: str) -> ApplicationUsage:
        return ApplicationUsage(application_id, 0.0, 1.0, ModelUsage(), {})

    async def prune(self, limit: int | None = None) -> int:
        return 0

    async def sweep(self) -> list[Expired]:
        self.sweeps += 1
        return self.expired.pop(0) if self.expired else []

    async def aclose(self) -> None:
        return None

    def each_ended_once(self) -> bool:
        """Every reservation ended exactly once, by a settle or a release."""
        ended = [i for _, i in self.ends]
        return sorted(ended) == [i for i, _, _ in self.reserved]


@dataclass
class Gateway:
    app: FastAPI
    client: httpx.AsyncClient
    clock: SimClock
    key: str
    budgets: RecordingBudgets
    provider: ScriptedProvider

    async def generate(self, **body: Any) -> httpx.Response:
        body.setdefault("model", "economy")
        body.setdefault("prompt", PROMPT)
        return await self.client.post("/v1/generate", json=body, headers=bearer(self.key))

    async def chat(self, **body: Any) -> httpx.Response:
        body.setdefault("model", "economy")
        body.setdefault("messages", [{"role": "user", "content": PROMPT}])
        return await self.client.post("/v1/chat/completions", json=body, headers=bearer(self.key))


@asynccontextmanager
async def gateway(
    *,
    budgets: RecordingBudgets | None = None,
    timeout_ms: float = TIMEOUT_MS,
    max_queue_len: Mapping[Tier, int] | None = None,
    adapter_tier: str | None = None,
    replies: list[Reply] | None = None,
    limits: Mapping[str, tuple[int | None, int | None]] | None = None,
    events: EventBus | None = None,
) -> AsyncIterator[Gateway]:
    clock = SimClock()
    registry = build_continuous_registry(
        clock, seed=7, max_queue_len=max_queue_len, profiles=TEST_PROFILES
    )
    access, keys = application_access(clock, apps={"acme": "enterprise"}, limits=limits)
    book = budgets if budgets is not None else RecordingBudgets()
    provider = ScriptedProvider(clock, list(replies or []))
    adapters: dict[str, Backend] = {}
    if adapter_tier is not None:
        adapters[adapter_tier] = OpenAICompatibleAdapter(
            name=adapter_tier,
            base_url="http://127.0.0.1:8081/v1",
            model="local-model",
            client=provider.client(),
        )
    app = create_app(
        registry,
        clock,
        applications=access,
        overload=OverloadPolicy(request_timeout_ms=timeout_ms),
        chat_adapters=adapters,
        budgets=book,
        reservation_bounds=BOUNDS,
        events=events,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(app, client, clock, keys["acme"], book, provider)
    await registry.aclose()


async def hang_up(g: Gateway, path: str, body: dict[str, Any], after_chunks: int) -> None:
    """Send a streaming request straight to the app, and hang up after some chunks."""
    raw = json.dumps({**body, "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {g.key}".encode()),
        ],
        "client": ("test-client", 1),
        "server": ("gateway", 80),
    }
    disconnected = asyncio.Event()
    sent = False
    chunks: list[bytes] = []

    async def receive() -> MutableMapping[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if len(chunks) == after_chunks:
                disconnected.set()

    await g.app(scope, receive, send)
    await g.clock.sleep_ms(1_000.0)
    assert len(chunks) >= after_chunks


# --- every way a request ends: settled if it billed, released otherwise ----------------------


async def test_a_generate_request_that_finishes_is_settled() -> None:
    async with gateway() as g:
        assert (await g.generate(max_tokens=4)).status_code == 200
        assert (await g.generate(max_tokens=4, stream=True)).status_code == 200
    assert g.budgets.ends == [("settle", 1), ("settle", 2)]


async def test_a_generate_request_that_times_out_is_released() -> None:
    async with gateway(timeout_ms=1.0) as g:
        answer = await g.generate(max_tokens=200)
    assert answer.json()["error"]["code"] == "request_timeout"
    assert g.budgets.ends == [("release", 1)]


async def test_requests_refused_by_a_full_queue_are_released() -> None:
    async with gateway(max_queue_len={Tier.PREMIUM: 0}) as g:
        # Premium seats 8 at a time and may queue none more.
        answers = await asyncio.gather(
            *(g.generate(model="premium", max_tokens=8) for _ in range(20))
        )
    statuses = [a.status_code for a in answers]
    assert 429 in statuses
    assert 200 in statuses
    settled = sorted(i for kind, i in g.budgets.ends if kind == "settle")
    released = sorted(i for kind, i in g.budgets.ends if kind == "release")
    assert len(settled) == statuses.count(200)
    assert len(released) == statuses.count(429)
    assert g.budgets.each_ended_once()


async def test_a_generate_stream_the_client_hangs_up_on_is_released() -> None:
    async with gateway() as g:
        await hang_up(g, "/v1/generate", {"model": "standard", "prompt": PROMPT}, 2)
    assert g.budgets.ends == [("release", 1)]


async def test_a_chat_request_that_finishes_is_settled() -> None:
    async with gateway() as g:
        assert (await g.chat(max_tokens=4)).status_code == 200
        assert (await g.chat(max_tokens=4, stream=True)).status_code == 200
    assert g.budgets.ends == [("settle", 1), ("settle", 2)]


async def test_a_chat_request_its_provider_fails_is_released() -> None:
    async with gateway(adapter_tier="economy", replies=[Reply(status=500)]) as g:
        answer = await g.chat()
    assert answer.status_code >= 500, answer.text
    assert g.budgets.ends == [("release", 1)]


async def test_a_chat_stream_that_breaks_after_output_is_released() -> None:
    """After output has started a failure bills nothing today: so it releases."""
    broken = openai_text(*["word "] * 20)
    broken.fail_after = 4
    async with gateway(adapter_tier="economy", replies=[broken]) as g:
        answer = await g.chat(stream=True)
    assert answer.status_code == 200
    assert "[DONE]" not in answer.text
    assert g.budgets.ends == [("release", 1)]


async def test_a_chat_stream_the_client_hangs_up_on_is_released() -> None:
    long = openai_text(*["word "] * 200)
    async with gateway(adapter_tier="economy", replies=[long]) as g:
        body = {"model": "economy", "messages": [{"role": "user", "content": PROMPT}]}
        await hang_up(g, "/v1/chat/completions", body, 4)
    assert g.budgets.ends == [("release", 1)]


async def test_a_chat_request_refused_after_admission_is_released() -> None:
    """The simulator takes no tools: refused (422) once the reservation is made."""
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    async with gateway() as g:
        answer = await g.chat(tools=[tool])
    assert answer.json()["error"]["code"] == "tools_not_supported", answer.text
    assert g.budgets.ends == [("release", 1)]


async def test_a_refused_reservation_holds_nothing_and_says_when_room_returns() -> None:
    refusal = BudgetExceededError("acme", 90, 100, retry_after_ms=12_300.0)
    async with gateway(budgets=RecordingBudgets(refuse=refusal)) as g:
        generated = await g.generate()
        chatted = await g.chat()
    assert generated.status_code == chatted.status_code == 429
    assert generated.json()["error"]["code"] == "budget_exhausted"
    assert chatted.json()["error"]["code"] == "budget_exhausted"
    assert generated.headers["retry-after"] == chatted.headers["retry-after"] == "13"
    assert g.budgets.ends == []


async def test_a_refusal_by_requests_in_progress_says_so_and_keeps_its_code_and_retry_after() -> (
    None
):
    """The in-flight 429: the same code, fields and Retry-After; the words say that tokens are
    held by requests in progress. A budget really used up still says when it resets."""
    held = BudgetExceededError("acme", 30, 100, retry_after_ms=4_200.0, held_tokens=80)
    async with gateway(budgets=RecordingBudgets(refuse=held)) as g:
        generated, chatted = await g.generate(), await g.chat()
    expected = (
        "Application 'acme' has used 30 of its 100 token budget for this period, and 80 tokens "
        "are held by requests in progress. Retry in 5 seconds, when they have finished or expired."
    )
    assert generated.json()["error"]["message"] == chatted.json()["error"]["message"] == expected
    assert (
        generated.json()["error"]["code"] == chatted.json()["error"]["code"] == "budget_exhausted"
    )
    assert generated.headers["retry-after"] == chatted.headers["retry-after"] == "5"
    assert {k: v for k, v in generated.json()["error"].items() if k != "message"} == {
        "code": "budget_exhausted",
        "used_tokens": 30,
        "budget_tokens": 100,
        "retry_after_s": 5,
    }
    used_up = BudgetExceededError("acme", 100, 100, retry_after_ms=4_200.0, held_tokens=80)
    assert not used_up.in_flight
    assert used_up.when(5) == "The budget resets in 5 seconds."
    assert (
        str(used_up) == "Application 'acme' has used 100 of its 100 token budget for this period."
    )


# --- the bound (Q2, B4) -----------------------------------------------------------------------


async def test_a_request_reserves_its_prompt_and_its_completion_capped_at_the_context() -> None:
    async with gateway(limits={"acme": (None, None)}) as g:
        await g.generate(prompt="abc", max_tokens=10)  # its own max_tokens
        await g.generate(prompt="abc")  # none: the tier's default
        await g.generate(prompt="\u00e9t\u00e9")  # bytes, not characters: 5
        await g.generate(prompt="x " * 3000, max_tokens=8)  # past the context
        messages = [{"role": "user", "content": "abc"}]
        await g.chat(messages=messages, max_tokens=5)
    tokens = [t for _, t, _ in g.budgets.reserved]
    chat_bytes = len(json.dumps([messages, None]).encode())
    assert tokens == [13, 3 + 256, 5 + 256, 4096, chat_bytes + 5]
    assert {hold for _, _, hold in g.budgets.reserved} == {TIMEOUT_MS + RESERVATION_MARGIN_MS}


async def test_a_plans_max_tokens_is_the_bound_when_the_request_names_none() -> None:
    async with gateway(limits={"acme": (50, None)}) as g:
        await g.generate(prompt="abc")
        await g.chat(messages=[{"role": "user", "content": "abc"}])
    generate, chat = (t for _, t, _ in g.budgets.reserved)
    assert generate == 3 + 50
    assert chat == len(json.dumps([[{"role": "user", "content": "abc"}], None]).encode()) + 50


def test_shared_budgets_need_a_timeout_and_every_tiers_settings() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
    access, _ = application_access(clock, apps={"acme": "enterprise"})
    with pytest.raises(ValueError, match="request timeout"):
        create_app(registry, clock, applications=access, budgets=RecordingBudgets())
    with pytest.raises(ValueError, match="missing: premium"):
        create_app(
            registry,
            clock,
            applications=access,
            overload=OverloadPolicy(request_timeout_ms=TIMEOUT_MS),
            budgets=RecordingBudgets(),
            reservation_bounds={k: v for k, v in BOUNDS.items() if k != "premium"},
        )


def test_the_tier_settings_are_read_and_checked() -> None:
    ok = parse_backend_config(
        {"budgets": {"economy": {"context_tokens": 100, "reserve_completion_tokens": 10}}}
    )
    assert ok.budgets == {Tier.ECONOMY: TierBudget(100, 10)}
    assert parse_backend_config({}).budgets == {}
    for wrong, says in (
        ({"cheap": {}}, "not a tier"),
        ({"economy": {"context_tokens": 100}}, "reserve_completion_tokens must be"),
        ({"economy": {"context_tokens": 0, "reserve_completion_tokens": 1}}, "context_tokens"),
        ({"economy": {"context_tokens": 5, "reserve_completion_tokens": 9}}, "at most"),
        ({"economy": {"context_tokens": True, "reserve_completion_tokens": 1}}, "positive"),
        ({"economy": {"context_tokens": 9, "reserve_completion_tokens": 1, "x": 1}}, "unknown"),
    ):
        with pytest.raises(BackendConfigError, match=says):
            parse_backend_config({"budgets": wrong})


# --- one process: the ledger reserves nothing, as before ------------------------------------


async def test_the_ledger_reserves_nothing_and_answers_as_check_then_record() -> None:
    clock = SimClock()
    ledger = CostLedger(clock, epoch=ClockEpoch(clock))
    reservation = await ledger.reserve("acme", 10, 9999, hold_ms=1.0)
    assert reservation == Reservation("acme", 9999, None)
    result = RequestResult("r1", "economy", 0.0, 0.0, 0.0, 1.0, 4, 8, 0.0)
    await ledger.settle(reservation, result, model="economy", price_multiplier=1.0)
    await ledger.release(reservation)  # nothing held: nothing to give back
    assert (await ledger.usage("acme")).total.billed_tokens == 12
    with pytest.raises(BudgetExceededError):
        await ledger.reserve("acme", 12, 1, hold_ms=1.0)
    await ledger.reserve("acme", 13, 10**6, hold_ms=1.0)  # the bound is not counted


# --- the sweep beside the server ----------------------------------------------------------------


async def test_the_sweep_runs_every_30_seconds_and_publishes_what_it_released() -> None:
    budgets = RecordingBudgets(expired=[[], [Expired("acme", 77)]])
    bus = EventBus(SimClock())
    async with gateway(budgets=budgets, events=bus) as g, g.app.router.lifespan_context(g.app):
        await g.clock.sleep_ms(30_000.0 * 2 + 1)
    assert budgets.sweeps == 2
    [event] = [e for e in bus.recent(100) if e.type == "reservation_expired"]
    assert (event.application_id, event.data) == ("acme", {"tokens": 77})


async def test_a_sweep_the_store_cannot_make_is_tried_at_the_next() -> None:
    class Down(RecordingBudgets):
        async def sweep(self) -> list[Expired]:
            self.sweeps += 1
            if self.sweeps == 1:
                raise StateUnavailableError("ConnectionRefusedError")
            return [Expired("acme", 1)]

    clock = SimClock()
    budgets = Down()
    seen: list[Expired] = []
    task = asyncio.ensure_future(run_sweep(budgets, clock, seen.append, every_ms=10.0))
    await clock.sleep_ms(25.0)
    task.cancel()
    assert budgets.sweeps == 2
    assert seen == [Expired("acme", 1)]


async def test_a_budget_write_kept_for_later_makes_the_instance_not_ready() -> None:
    async with gateway(budgets=RecordingBudgets(healthy=False)) as g:
        answer = await g.client.get("/readyz")
    assert (answer.status_code, answer.json()) == (503, {"status": "not_ready"})


# --- the settings that let shared mode start ---------------------------------------------------


def test_shared_mode_needs_postgres_budgets_and_valkey_counters() -> None:
    url = "postgresql://gw:pw@127.0.0.1:5432/gw"
    check_state_setting({})
    check_state_setting({STATE_ENV: "sqlite"})
    with pytest.raises(SharedStateRefusedError, match=f"needs {POSTGRES_URL_ENV}: budgets"):
        check_state_setting({STATE_ENV: "shared", COUNTERS_ENV: "valkey"})
    with pytest.raises(SharedStateRefusedError, match=f"needs {COUNTERS_ENV}=valkey"):
        check_state_setting({STATE_ENV: "shared", POSTGRES_URL_ENV: url})
    with pytest.raises(SharedStateRefusedError, match="valkey"):
        check_state_setting({STATE_ENV: "shared", POSTGRES_URL_ENV: url, COUNTERS_ENV: "memory"})
    check_state_setting({STATE_ENV: "shared", POSTGRES_URL_ENV: url, COUNTERS_ENV: "valkey"})
    with pytest.raises(ValueError, match="sqlite or shared"):
        check_state_setting({STATE_ENV: "postgres"})


LOCAL = (
    "postgresql://gw:pw@127.0.0.1:5432/gw",
    "postgresql://gw:pw@localhost/gw",
    "postgresql://gw:pw@[::1]:5432/gw",
    "postgresql://gw:pw@/gw?host=/var/run/postgresql",
    "postgresql://gw:pw@127.0.0.1/gw?host=127.0.0.2",
)
REMOTE = (
    "postgresql://gw:pw@db.example.test:5432/gw",
    "postgresql://gw:pw@10.0.0.5/gw",
    "postgresql://gw:pw@db.example.test/gw?sslmode=require",
    "postgresql://gw:pw@db.example.test/gw?sslmode=verify-ca",
    "postgresql://gw:pw@127.0.0.1/gw?host=db.example.test",
    "postgresql://gw:pw@127.0.0.1/gw?host=127.0.0.1,db.example.test",
)


@pytest.mark.parametrize("url", LOCAL)
def test_a_postgres_on_this_machine_needs_no_tls(url: str) -> None:
    check_store_address("postgres", url, POSTGRES_URL_ENV, {})


@pytest.mark.parametrize("url", REMOTE)
def test_a_postgres_elsewhere_needs_verified_tls_or_the_operators_word(url: str) -> None:
    with pytest.raises(InsecureStoreAddressError) as refused:
        check_store_address("postgres", url, POSTGRES_URL_ENV, {})
    message = str(refused.value)
    assert POSTGRES_URL_ENV in message
    assert PRIVATE_NETWORK_ENV in message
    assert "example" not in message
    assert "10.0.0.5" not in message
    assert "pw" not in message
    check_store_address("postgres", url, POSTGRES_URL_ENV, {PRIVATE_NETWORK_ENV: "1"})
    with pytest.raises(InsecureStoreAddressError):
        check_store_address("postgres", url, POSTGRES_URL_ENV, {PRIVATE_NETWORK_ENV: "yes"})


def test_a_postgres_elsewhere_with_verify_full_is_accepted() -> None:
    url = "postgresql://gw:pw@db.example.test:5432/gw?sslmode=verify-full"
    check_store_address("postgres", url, POSTGRES_URL_ENV, {})


def test_a_valkey_elsewhere_needs_rediss_with_its_checks_left_on() -> None:
    def check(url: str, environ: Mapping[str, str] | None = None) -> None:
        check_store_address("valkey", url, VALKEY_URL_SETTING, environ or {})

    check("redis://127.0.0.1:6380/0")
    check("redis://:pw@localhost:6380/0")
    check("rediss://cache.example.test:6380/0")
    check("rediss://cache.example.test:6380/0?ssl_cert_reqs=required")
    for url in (
        "redis://cache.example.test:6380/0",
        "rediss://cache.example.test:6380/0?ssl_cert_reqs=none",
        "rediss://cache.example.test:6380/0?ssl_cert_reqs=optional",
        "rediss://cache.example.test:6380/0?ssl_check_hostname=false",
    ):
        with pytest.raises(InsecureStoreAddressError) as refused:
            check(url)
        assert "example" not in str(refused.value)
        check(url, {PRIVATE_NETWORK_ENV: "1"})


def _shared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, budgets: bool = True
) -> None:
    backends = tmp_path / "backends.yaml"
    tiers = {t.value: {"context_tokens": 4096, "reserve_completion_tokens": 256} for t in Tier}
    backends.write_text(
        json.dumps({"backend": "simulated", **({"budgets": tiers} if budgets else {})}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BACKENDS", str(backends))
    monkeypatch.setenv(STATE_ENV, "shared")
    monkeypatch.setenv(POSTGRES_URL_ENV, "postgresql://gw:planted-pw@127.0.0.1:1/gw")
    monkeypatch.setenv(COUNTERS_ENV, "valkey")
    monkeypatch.setenv(VALKEY_URL_SETTING, "redis://127.0.0.1:1/0")


def _no_connection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    tried: list[str] = []

    async def create_pool(*_: Any, **__: Any) -> None:
        tried.append("create_pool")

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return tried


def test_a_shared_server_is_built_without_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_environment(monkeypatch, tmp_path)
    tried = _no_connection(monkeypatch)
    app = create_default_app()
    assert tried == []  # the lifespan connects, on the server's loop
    assert not hasattr(app.state, "database_lock")  # several processes share the database
    assert not (tmp_path / "gateway.db").exists()


def test_a_shared_server_refuses_what_it_cannot_run_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tried = _no_connection(monkeypatch)
    _shared_environment(monkeypatch, tmp_path, budgets=False)
    with pytest.raises(SharedStateRefusedError, match="missing: economy, standard, premium"):
        create_default_app()
    _shared_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("GATEWAY_SIMULATED_LIVE", "1")
    with pytest.raises(SharedStateRefusedError, match="simulated live"):
        create_default_app()
    monkeypatch.delenv("GATEWAY_SIMULATED_LIVE")
    monkeypatch.setenv(POSTGRES_URL_ENV, "postgresql://gw:planted-pw@db.example.test/gw")
    with pytest.raises(InsecureStoreAddressError, match=POSTGRES_URL_ENV) as postgres:
        create_default_app()
    monkeypatch.setenv(POSTGRES_URL_ENV, "postgresql://gw:planted-pw@127.0.0.1:1/gw")
    monkeypatch.setenv(VALKEY_URL_SETTING, "redis://:planted-pw@cache.example.test:6380/0")
    with pytest.raises(InsecureStoreAddressError, match=VALKEY_URL_SETTING) as valkey:
        create_default_app()
    monkeypatch.delenv(POSTGRES_URL_ENV)
    with pytest.raises(SharedStateRefusedError, match="budgets"):
        create_default_app()
    for refused in (postgres, valkey):
        assert "planted-pw" not in str(refused.value)
        assert "example" not in str(refused.value)
    assert "planted-pw" not in caplog.text
    assert tried == []
    assert not (tmp_path / "gateway.db").exists()


# --- Valkey's clock against Postgres's (section 3.8), both under the test's control -----------


async def _ignore_restart(_: str) -> None:
    return None


def healthy(counters: ValkeyCounters) -> bool:
    return counters.healthy


def _watched(
    client: ScriptedClient, epoch: ManualEpoch, reference_ms: list[float], clock: Any
) -> ValkeyCounters:
    async def reference() -> float:
        return reference_ms[0]

    return ValkeyCounters(
        client,  # type: ignore[arg-type]
        clock,
        epoch,
        TEST_PEPPER,
        prefix="gwtest:scripted:",
        reference=reference,
    )


async def test_the_skew_is_measured_against_postgres_not_valkey() -> None:
    """With Postgres configured, Valkey's own clock does not count: far off, it is not used;
    Postgres's, just past the bound, refuses the start and names whose clock it is."""
    client = ScriptedClient()
    epoch = ManualEpoch(1.75e12)
    client.time_ms = epoch.now_ms + 60_000.0
    reference = [epoch.now_ms + 999.0]
    counters = _watched(client, epoch, reference, SimClock())
    await counters.watch.start(_ignore_restart)
    assert counters.healthy
    assert counters.watch.skew_ms == 999.0
    reference[0] = epoch.now_ms - 1001.0
    late = _watched(client, epoch, reference, SimClock())
    with pytest.raises(StateUnavailableError, match="1001 ms from Postgres's"):
        await late.watch.start(_ignore_restart)


async def test_drift_past_the_bound_stops_the_valkey_windows_until_it_is_back() -> None:
    """The periodic check: every 30 s on the injected clock. Drift past the bound refuses every
    request that would use a Valkey window, and the instance is not ready; back inside, both
    come back at the next check."""
    clock = SimClock()
    client = ScriptedClient()
    epoch = ManualEpoch(1.75e12)
    reference = [epoch.now_ms]
    counters = _watched(client, epoch, reference, clock)
    await counters.watch.start(_ignore_restart)
    watching = asyncio.ensure_future(counters.watch.run(_ignore_restart))
    try:
        reference[0] = epoch.now_ms + 1500.0
        await clock.sleep_ms(29_000.0)
        assert healthy(counters)  # not checked yet
        await clock.sleep_ms(2_000.0)
        assert not healthy(counters)
        with pytest.raises(StateUnavailableError, match="clock_skew"):
            await counters.rate_limiter().admit("acme", 10)
        reference[0] = epoch.now_ms + 10.0
        await clock.sleep_ms(30_000.0)
        assert healthy(counters)
    finally:
        watching.cancel()
