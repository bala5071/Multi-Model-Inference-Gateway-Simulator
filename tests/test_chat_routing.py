"""Chat requests routed among the gateway's tier and the organization's providers: the modes,
the five fallback triggers, what never happens, the circuit breaker, the headers and the usage
record, the mode endpoints, and that ``local_only`` never reaches a hosted provider on any path.
See docs/33-fallback-modes.md. No network: the operator's server and the organization's
providers are scripted networks behind the real egress guard. Test values, not secrets."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from accounts_helpers import PASSWORD
from gateway.adapters import OpenAICompatibleAdapter
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app
from gateway.audit import AuditAction
from gateway.auth import AccountAccess
from gateway.backends import Backend
from gateway.chatrouter import ChatRouter
from gateway.continuous import build_continuous_registry
from gateway.egress import LocalDestinations, operator_client, organization_client
from gateway.events import EventBus
from gateway.localonly import LocalOnlyPolicy
from gateway.modes import Mode, SqliteModeStore, effective_mode, looser_than
from gateway.providers import ProviderConfig, ProviderService, ProviderStore
from gateway.redaction import Redactor
from gateway.usage import ChatSource, SqliteUsageStore
from gateway.vault import Vault
from mock_network import HANG, Breaks, FakeResolver, Gate, ScriptedNetwork, http_response
from mock_network import sse_response as sse
from mock_providers import openai_text, openai_tools
from test_management_api import FORBIDDEN, World, make_world

REPO = Path(__file__).resolve().parent.parent
MASTER = bytes(range(32))
KEY = "sk-planted-routing-key-0123456789abcdefghij"
OPERATOR = "http://127.0.0.1:8081/v1"
LOCAL = LocalDestinations.parse("127.0.0.1:8081")
HOSTS = {
    "a.provider.test": ["93.184.216.34"],
    "b.provider.test": ["93.184.216.35"],
    "p.provider.test": ["93.184.216.36"],
    "api.operator.test": ["93.184.216.40"],
}
A, B, P = "prv_00000000000000a1", "prv_00000000000000b2", "prv_00000000000000c3"
TEXT = sse(openai_text("Hello").chunks)
FIVE_XX = http_response(b'{"error": {"message": "boom"}}', "500 Internal Server Error")
UNAUTHORIZED = http_response(b'{"error": {"message": "bad key"}}', "401 Unauthorized")
RATE_LIMITED = http_response(b"{}", "429 Too Many Requests", Retry_After="7")
REJECTED = http_response(b'{"error": {"type": "invalid_request_error"}}', "400 Bad Request")
BAD_TOOL = sse(openai_tools(("call_1", "ping", ['{"x": '])).chunks)
GOOD_TOOL = sse(openai_tools(("call_1", "ping", ["{}"])).chunks)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Answers pong.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
_partial = openai_text("He", "llo").chunks
BROKEN_AFTER_OUTPUT = Breaks(
    http_response(b"", Content_Type="text/event-stream") + _partial[0][1] + _partial[1][1]
)
LOCAL_ONLY = Mode.LOCAL_ONLY
FALLBACK = Mode.LOCAL_WITH_HOSTED_FALLBACK
PRIMARY = Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK


def refused() -> Exception:
    return httpcore.ConnectError("[Errno 111] Connection refused")


@dataclass
class Routing:
    world: World
    service: ProviderService
    modes: SqliteModeStore
    org_net: ScriptedNetwork
    op_net: ScriptedNetwork
    resolver: FakeResolver
    client: httpx.AsyncClient
    router: ChatRouter
    bus: EventBus

    @property
    def clock(self) -> Any:
        return self.world.accounts.clock

    def provider(
        self,
        provider_id: str,
        host: str,
        *,
        tier: str = "economy",
        role: str = "primary",
        tools: bool | None = None,
        budget_ms: float | None = None,
        prices: tuple[float | None, float | None] = (None, None),
        key: str = KEY,
    ) -> ProviderConfig:
        """A provider of north's, with its key, written straight to the store."""
        capabilities = (
            {}
            if tools is None
            else {"supports_tools": {"value": tools, "source": "probed", "reason": None}}
        )
        config = ProviderConfig(
            provider_id, "north", "openai_compatible", f"https://{host}/v1", "model-x", tier,
            role, True, True, prices[0], prices[1], capabilities, budget_ms, 0.0,
        )  # fmt: skip
        self.service.store.add(config)
        self.service.store.put_key(provider_id, self.service.vault.seal(key, config.binding), 1.0)
        return config

    def mode(self, organization: Mode, application: Mode | None = None) -> None:
        self.modes.set_organization_mode("north", organization, 0.0)
        self.modes.set_application_mode("north-shop", application or organization, 0.0)

    async def chat(
        self, model: str = "economy", *, stream: bool = False, tools: bool = False
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Say hello."}],
            "stream": stream,
        }
        if tools:
            body["tools"] = TOOLS
        return await self.client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.world.api_keys['north-shop']}"},
        )

    def hosted_contacted(self) -> list[str]:
        """Every address an organization provider was connected to, and every provider host
        name that was even resolved."""
        names = [n for n in self.resolver.calls if n.endswith(".provider.test")]
        return [c.address for c in self.org_net.connections] + names

    def last_source(self) -> ChatSource | None:
        events = SqliteUsageStore(self.world.accounts.connection).events("north-shop")
        return events[-1].event.source

    # --- admin ---------------------------------------------------------------------------

    async def login(self, username: str) -> tuple[str, str]:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def step_up(self, session: tuple[str, str]) -> None:
        response = await self.client.post(
            "/v1/auth/step-up", json={"password": PASSWORD}, headers=_headers(session)
        )
        assert response.status_code == 200, response.text

    async def put_mode(self, session: tuple[str, str], path: str, mode: str) -> httpx.Response:
        return await self.client.put(
            "/v1/admin/orgs" + path, json={"mode": mode}, headers=_headers(session)
        )


def _headers(session: tuple[str, str]) -> dict[str, str]:
    return {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}


@asynccontextmanager
async def routing(
    *, operator: str | None = OPERATOR, local: LocalDestinations = LOCAL
) -> AsyncIterator[Routing]:
    """North's applications, the economy tier served by the operator's server at ``operator``
    (the simulator when None), and the organization's providers on another scripted network."""
    world = make_world()
    accounts = world.accounts
    clock = accounts.clock
    resolver = FakeResolver(dict(HOSTS))
    org_net, op_net = ScriptedNetwork(), ScriptedNetwork()
    providers = ProviderService(
        ProviderStore(accounts.connection),
        Vault(MASTER),
        accounts.audit,
        clock,
        accounts.epoch,
        Redactor(),
        lambda: organization_client(resolver, inner=org_net),
    )
    adapters: dict[str, Backend] = {}
    if operator is not None:
        adapters["economy"] = OpenAICompatibleAdapter(
            name="economy",
            base_url=operator,
            model="local-model",
            client=operator_client(resolver, local, inner=op_net),
        )
    modes = SqliteModeStore(accounts.connection)
    bus = EventBus(clock)
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        providers=providers,
        chat_adapters=adapters,
        modes=modes,
        local_destinations=local,
        events=bus,
        usage_store=SqliteUsageStore(accounts.connection),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Routing(
            world, providers, modes, org_net, op_net, resolver, client, app.state.chat_router, bus
        )
    await registry.aclose()


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def _source(response: httpx.Response) -> tuple[str, str, str | None, str | None]:
    h = response.headers
    return (
        h["x-provider"],
        h["x-provider-class"],
        h.get("x-fallback-from"),
        h.get("x-fallback-reason"),
    )


# --- the mode, decided in one place --------------------------------------------------------


def test_the_effective_mode_is_the_stricter_of_the_two() -> None:
    order = [LOCAL_ONLY, FALLBACK, PRIMARY]
    for i, organization in enumerate(order):
        for j, application in enumerate(order):
            assert effective_mode(organization, application) is order[min(i, j)]
            assert looser_than(application, organization) is (j > i)


def test_the_effective_mode_is_decided_in_one_place() -> None:
    """Only gateway.modes defines how modes compare; everything else asks effective_mode."""
    defined: list[str] = []
    strictness: list[str] = []
    for path in sorted((REPO / "src" / "gateway").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "effective_mode":
                defined.append(path.name)
            if isinstance(node, ast.Name) and node.id == "_STRICTNESS":
                strictness.append(path.name)
    assert defined == ["modes.py"]
    assert set(strictness) == {"modes.py"}


# --- local_only never reaches a hosted provider --------------------------------------------


async def test_local_only_is_the_default_and_answers_locally() -> None:
    async with routing() as r:
        r.provider(A, "a.provider.test", role="primary")
        r.provider(B, "b.provider.test", role="fallback")
        r.op_net.responses = [TEXT]
        response = await r.chat()
        source = r.last_source()
    assert response.status_code == 200, response.text
    assert _source(response) == ("local", "local", None, None)
    assert source == ChatSource("local", "local", None, None, False)
    assert r.hosted_contacted() == []


async def test_local_only_never_reaches_hosted_on_any_path() -> None:
    """Hosted providers are configured for the tier, and the local tier fails in every way
    that would be a fallback trigger in another mode, including a retry (invalid tool JSON)
    and with a provider's breaker just reset to half-open, streaming and not. Not one hosted
    connection, not one hosted name resolved."""
    async with routing() as r:
        r.provider(A, "a.provider.test", role="primary", tools=True)
        r.provider(B, "b.provider.test", role="fallback", tools=True)
        r.org_net.responses = [TEXT, GOOD_TOOL] * 10
        # A's breaker: opened, then its time passed, so it would admit one probe now.
        for _ in range(5):
            r.router.breakers.failure("north", A)
        await r.clock.sleep_ms(31_000.0)
        answers = []
        for stream in (False, True):
            for scripted, tools in (
                ([refused()], False),
                ([FIVE_XX], False),
                ([HANG], False),
                ([BAD_TOOL, BAD_TOOL], True),
            ):
                r.op_net.responses = list(scripted)
                answers.append(await r.chat(stream=stream, tools=tools))
        assert r.router.breakers.admit("north", A)  # the reset breaker was never used
    assert [a.status_code for a in answers] == [502] * 8
    assert r.hosted_contacted() == []


async def test_a_mode_made_stricter_mid_request_stops_the_fallback() -> None:
    """The request starts under local_with_hosted_fallback; while the local server is being
    connected to, the organization becomes local_only; the local failure then leads nowhere.
    The control, without the change, falls back to the hosted provider."""
    for change in (False, True):
        async with routing() as r:
            r.mode(FALLBACK)
            r.provider(B, "b.provider.test", role="fallback")
            r.org_net.responses = [TEXT]
            gate = Gate(refused())
            r.op_net.responses = [gate]
            request = asyncio.ensure_future(r.chat())
            await gate.reached.wait()
            if change:
                r.modes.set_organization_mode("north", LOCAL_ONLY, 1.0)
            gate.opened.set()
            response = await request
        if change:
            assert (response.status_code, _code(response)) == (502, "upstream_unavailable")
            assert r.hosted_contacted() == []
        else:
            assert response.status_code == 200, response.text
            assert _source(response) == (B, "hosted", "local", "connect_error")


async def test_a_mode_made_stricter_mid_request_is_refused_by_the_guard_too() -> None:
    """The second layer: even with the router's own check passed, the guard asks the mode
    again when the hosted connection is opened."""
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary")
        gate = Gate(TEXT)
        r.org_net.responses = [gate]
        r.op_net.responses = [TEXT]

        async def resolve_then_tighten(host: str, port: int) -> list[str]:
            r.modes.set_organization_mode("north", LOCAL_ONLY, 1.0)
            return list(HOSTS[host])

        r.resolver.table.clear()
        guarded = r.service.client_factory

        def tightening_client() -> httpx.AsyncClient:
            return organization_client(resolve_then_tighten, inner=r.org_net)

        r.router._client_factory = tightening_client
        response = await r.chat()
        r.router._client_factory = guarded
    # The resolver ran (the router allowed it), the mode changed, the connect was refused,
    # and the request fell back to local: reason connect_error, nothing sent to A.
    assert response.status_code == 200, response.text
    assert _source(response) == ("local", "local", A, "connect_error")
    assert r.org_net.connections == []
    assert not gate.reached.is_set()


# --- the triggers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scripted", "tools", "reason"),
    [
        ([refused()], False, "connect_error"),
        ([FIVE_XX], False, "upstream_5xx"),
        ([HANG], False, "first_token_budget"),
        ([BAD_TOOL, BAD_TOOL], True, "invalid_tool_json"),
    ],
    ids=["connect_error", "upstream_5xx", "first_token_budget", "invalid_tool_json"],
)
async def test_each_trigger_falls_back_from_local_to_hosted(
    scripted: list[Any], tools: bool, reason: str
) -> None:
    async with routing() as r:
        r.mode(FALLBACK)
        r.provider(B, "b.provider.test", role="fallback", tools=True)
        r.op_net.responses = list(scripted)
        r.org_net.responses = [GOOD_TOOL if tools else TEXT]
        response = await r.chat(tools=tools)
        source = r.last_source()
    assert response.status_code == 200, response.text
    assert _source(response) == (B, "hosted", "local", reason)
    assert source == ChatSource(B, "hosted", "local", reason, False)
    assert len(r.op_net.connections) == len(scripted)  # invalid tool JSON: tried twice


async def test_an_open_breaker_skips_the_provider_without_contacting_it() -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary")
        for _ in range(5):
            r.router.breakers.failure("north", A)
        r.op_net.responses = [TEXT]
        response = await r.chat()
    assert response.status_code == 200, response.text
    assert _source(response) == ("local", "local", A, "circuit_open")
    assert r.hosted_contacted() == []


async def test_invalid_tool_json_is_retried_once_on_the_same_provider() -> None:
    async with routing(operator=None) as r:  # the simulator: no tools, so not a candidate
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary", tools=True)
        r.provider(B, "b.provider.test", role="primary", tools=True)
        r.org_net.responses = [BAD_TOOL, GOOD_TOOL]
        retried = await r.chat(tools=True)
        r.org_net.responses = [BAD_TOOL, BAD_TOOL, GOOD_TOOL]
        moved_on = await r.chat(tools=True)
    assert retried.status_code == 200, retried.text
    assert _source(retried) == (A, "hosted", None, None)
    assert moved_on.status_code == 200, moved_on.text
    assert _source(moved_on) == (B, "hosted", A, "invalid_tool_json")
    assert [c.address for c in r.org_net.connections] == ["93.184.216.34"] * 4 + ["93.184.216.35"]


@pytest.mark.parametrize(
    ("scripted", "status", "code"),
    [
        (UNAUTHORIZED, 502, "provider_auth_failed"),
        (RATE_LIMITED, 429, "upstream_rate_limited"),
        (REJECTED, 502, "upstream_rejected"),
    ],
    ids=["401", "429", "4xx"],
)
async def test_what_is_not_a_trigger_is_the_answer(scripted: bytes, status: int, code: str) -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary")
        r.org_net.responses = [scripted]
        r.op_net.responses = [TEXT]
        response = await r.chat()
    assert (response.status_code, _code(response)) == (status, code)
    assert r.op_net.connections == []  # local never tried
    assert KEY not in response.text


# --- what never happens ---------------------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False], ids=["streaming", "not_streaming"])
async def test_never_after_output_has_started(stream: bool) -> None:
    """The primary sends "He", then its connection breaks (a trigger kind of failure): the
    request fails, and the local tier is never tried."""
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary")
        r.org_net.responses = [BROKEN_AFTER_OUTPUT]
        r.op_net.responses = [TEXT]
        response = await r.chat(stream=stream)
    if stream:
        assert response.status_code == 200
        assert response.headers["x-provider"] == A
        assert '"content": "He"' in response.text
        assert '"code":"upstream_unavailable"' in response.text
        assert "[DONE]" not in response.text
    else:
        assert (response.status_code, _code(response)) == (502, "upstream_unavailable")
    assert r.op_net.connections == []


async def test_never_another_tier() -> None:
    """A provider for premium is never a candidate for an economy request, even when the
    economy candidates fail or there are none."""
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(P, "p.provider.test", tier="premium", role="primary")
        r.op_net.responses = [TEXT]
        only_premium = await r.chat()
        r.provider(A, "a.provider.test", role="primary")
        r.org_net.responses = [FIVE_XX, TEXT]
        r.op_net.responses = [TEXT]
        economy_failed = await r.chat()
    assert _source(only_premium) == ("local", "local", None, None)
    assert _source(economy_failed) == ("local", "local", A, "upstream_5xx")
    assert "93.184.216.36" not in r.hosted_contacted()
    assert "p.provider.test" not in r.hosted_contacted()


async def test_never_a_model_outside_the_plan() -> None:
    """north-shop's plan has no premium: a provider serving premium changes nothing."""
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(P, "p.provider.test", tier="premium", role="primary")
        r.org_net.responses = [TEXT]
        response = await r.chat("premium")
    assert (response.status_code, _code(response)) == (403, "model_not_in_plan")
    assert r.hosted_contacted() == []


# --- headers, usage, prices, locality of the operator's tier --------------------------------


async def test_a_hosted_answer_is_charged_at_the_organizations_prices() -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary", prices=(2.0, 4.0))
        r.org_net.responses = [TEXT]
        response = await r.chat()
        events = SqliteUsageStore(r.world.accounts.connection).events("north-shop")
    assert response.status_code == 200, response.text
    assert _source(response) == (A, "hosted", None, None)
    event = events[-1].event
    assert (event.prompt_tokens, event.completion_tokens) == (9, 1)
    assert event.base_cost_usd == pytest.approx((9 * 2.0 + 1 * 4.0) / 1_000_000)
    assert event.source == ChatSource(A, "hosted", None, None, False)
    assert "a.provider.test" not in json.dumps(dict(response.headers))  # a class, never a URL


async def test_an_operator_tier_at_a_hosted_destination_is_hosted() -> None:
    """The operator's tier at a URL not on its local list is hosted: refused in local_only
    (no local candidate), served and labelled hosted when the mode allows hosted."""
    async with routing(operator="https://api.operator.test/v1") as r:
        r.op_net.responses = [TEXT]
        refused_ = await r.chat()
        r.mode(FALLBACK)
        served = await r.chat()
    assert (refused_.status_code, _code(refused_)) == (503, "no_local_provider")
    assert served.status_code == 200, served.text
    assert _source(served) == ("local", "hosted", None, None)
    assert [c.address for c in r.op_net.connections] == ["93.184.216.40"]


async def test_streaming_headers_name_the_provider_and_the_fallback() -> None:
    async with routing() as r:
        r.mode(FALLBACK)
        r.provider(B, "b.provider.test", role="fallback")
        r.op_net.responses = [FIVE_XX]
        r.org_net.responses = [TEXT]
        response = await r.chat(stream=True)
    assert response.status_code == 200
    assert _source(response) == (B, "hosted", "local", "upstream_5xx")
    assert "[DONE]" in response.text


# --- the breaker and the slow-provider warning, over HTTP -----------------------------------


async def test_the_breaker_opens_skips_probes_and_closes_with_events() -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary")
        r.org_net.responses = [FIVE_XX] * 5
        r.op_net.responses = [TEXT] * 9
        for _ in range(5):
            assert _source(await r.chat())[3] == "upstream_5xx"
        skipped = await r.chat()  # open: A is not contacted
        assert len(r.org_net.connections) == 5
        await r.clock.sleep_ms(30_000.0)
        r.org_net.responses = [TEXT, TEXT]
        probe = await r.chat()  # half-open: one request through, and it succeeds
        closing = await r.chat()  # the second success closes it
        states = [e.data["state"] for e in r.bus.recent(10) if e.type == "circuit_breaker"]
    assert _source(skipped) == ("local", "local", A, "circuit_open")
    assert _source(probe)[0] == A
    assert _source(closing)[0] == A
    assert states == ["open", "half_open", "closed"]


async def test_a_slow_provider_is_flagged_and_cleared() -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        r.provider(A, "a.provider.test", role="primary", budget_ms=2_000.0)
        r.org_net.responses = [HANG] * 3
        r.op_net.responses = [TEXT] * 3
        for _ in range(3):
            assert _source(await r.chat())[3] == "first_token_budget"
        alice = await r.login("alice")
        listed = (
            await r.client.get("/v1/admin/orgs/north/providers", headers=_headers(alice))
        ).json()["providers"][0]
        r.org_net.responses = [TEXT]
        await r.chat()
        cleared = (
            await r.client.get("/v1/admin/orgs/north/providers", headers=_headers(alice))
        ).json()["providers"][0]
    assert listed["consecutive_misses"] == 3
    assert listed["slow_since_ms"] is not None
    assert (cleared["consecutive_misses"], cleared["slow_since_ms"]) == (0, None)


# --- the mode endpoints ---------------------------------------------------------------------


async def test_each_mode_change_needs_its_own_password_and_is_audited() -> None:
    async with routing() as r:
        alice = await r.login("alice")
        await r.step_up(alice)
        first = await r.put_mode(alice, "/north/mode", "local_with_hosted_fallback")
        again = await r.put_mode(alice, "/north/mode", "hosted_primary_with_local_fallback")
        entries = [
            e.event
            for e in r.world.accounts.audit.entries()
            if e.event.action is AuditAction.MODE_CHANGED
        ]
    assert first.status_code == 200, first.text
    assert first.json() == {
        "organization_id": "north",
        "mode": "local_with_hosted_fallback",
        "previous_mode": "local_only",
    }
    assert (again.status_code, _code(again)) == (403, "step_up_required")
    [entry] = entries
    assert entry.target_id == "north"
    assert entry.details["old_mode"] == "local_only"
    assert entry.details["new_mode"] == "local_with_hosted_fallback"
    assert entry.details["step_up_consumed"] is True


async def test_an_application_may_not_be_looser_than_its_organization() -> None:
    """Refused with 409 before any change, so the window comes back and the next change
    needs no new password."""
    async with routing() as r:
        nina = await r.login("nina")  # app_admin of north-shop
        await r.step_up(nina)
        looser = await r.put_mode(
            nina, "/north/applications/north-shop/mode", "hosted_primary_with_local_fallback"
        )
        same = await r.put_mode(nina, "/north/applications/north-shop/mode", "local_only")
        r.modes.set_organization_mode("north", PRIMARY, 1.0)
        await r.step_up(nina)
        stricter = await r.put_mode(
            nina, "/north/applications/north-shop/mode", "local_with_hosted_fallback"
        )
    assert (looser.status_code, _code(looser)) == (409, "mode_looser_than_organization")
    assert same.status_code == 200, same.text
    assert stricter.json() == {
        "application_id": "north-shop",
        "mode": "local_with_hosted_fallback",
        "previous_mode": "local_only",
        "effective_mode": "local_with_hosted_fallback",
    }


async def test_an_organization_made_stricter_takes_its_applications_with_it() -> None:
    async with routing() as r:
        r.mode(FALLBACK)
        r.provider(B, "b.provider.test", role="fallback")
        r.op_net.responses = [FIVE_XX, FIVE_XX]
        r.org_net.responses = [TEXT]
        before = await r.chat()
        r.modes.set_organization_mode("north", LOCAL_ONLY, 1.0)
        after = await r.chat()
        stored = r.modes.application_mode("north-shop")
    assert _source(before) == (B, "hosted", "local", "upstream_5xx")
    assert (after.status_code, _code(after)) == (502, "upstream_unavailable")
    assert stored is FALLBACK  # its own value is unchanged; the stricter one applies
    assert len(r.org_net.connections) == 1


async def test_who_may_set_a_mode_and_missing_looks_forbidden() -> None:
    async with routing() as r:
        answers = {}
        for user, path in (
            ("bob", "/north/mode"),  # no role
            ("vera", "/north/mode"),  # viewer
            ("nina", "/north/mode"),  # app_admin: not the organization's
            ("sid", "/north/mode"),  # another organization's admin
            ("sid", "/north/applications/north-shop/mode"),
            ("alice", "/north/applications/no-such-app/mode"),
            ("alice", "/nowhere/mode"),
        ):
            session = await r.login(user)
            await r.step_up(session)
            answers[(user, path)] = await r.put_mode(session, path, "local_only")
    for response in answers.values():
        assert response.status_code == 403
        assert response.json() == FORBIDDEN
    assert len({r_.content for r_ in answers.values()}) == 1  # byte for byte


async def test_the_candidates_in_each_mode() -> None:
    async with routing() as r:
        r.provider(A, "a.provider.test", role="primary", tools=False)
        r.provider(B, "b.provider.test", role="fallback")
        r.provider(P, "p.provider.test", tier="premium", role="primary")

        def names(mode: Mode, *, tools: bool = False) -> list[tuple[str, str]]:
            r.mode(mode)
            found = r.router.candidates("north", "north-shop", "economy", wants_tools=tools)
            return [(c.provider, c.provider_class) for c in found]

        assert names(LOCAL_ONLY) == [("local", "local")]
        assert names(FALLBACK) == [("local", "local"), (B, "hosted")]
        assert names(PRIMARY) == [(A, "hosted"), ("local", "local")]
        # A says it cannot take tools; the operator's server has not said.
        assert names(PRIMARY, tools=True) == [("local", "local")]
        # A disabled, or without a key, is no candidate at all.
        r.service.store.delete_key(A)
        assert names(PRIMARY) == [("local", "local")]


async def test_a_provider_whose_key_cannot_be_read_is_skipped_never_contacted() -> None:
    async with routing() as r:
        r.mode(PRIMARY)
        config = r.provider(A, "a.provider.test", role="primary")
        # Sealed under a master key this gateway does not have (a lost one).
        r.service.store.put_key(A, Vault(bytes(range(32, 64))).seal(KEY, config.binding), 2.0)
        r.op_net.responses = [TEXT]
        response = await r.chat()
    assert response.status_code == 200, response.text
    assert _source(response) == ("local", "local", A, "key_unreadable")
    assert r.hosted_contacted() == []


def test_the_server_shares_one_tls_context_across_its_organization_clients() -> None:
    """A client is made for every call to an organization's provider; building a TLS context
    loads the certificate bundle, so the server makes one and passes it to all of them."""
    from gateway.app import create_default_app

    app = create_default_app()
    try:
        factory = app.state.providers.client_factory
        first, second = factory(), factory()
        contexts = [c._transport._pool._ssl_context for c in (first, second)]
    finally:
        app.state.database_lock.release()
    assert contexts[0] is contexts[1]
