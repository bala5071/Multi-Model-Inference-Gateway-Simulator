"""The "never" rules of chat routing, as invariants checked from outside the router: over random
plans, tiers, modes, provider lists and provider failures, no request is ever sent to a provider
or a model outside them. See docs/33-fallback-modes.md.

Nothing here asks the router what it meant to do. Each case looks only at what reached the two
scripted networks (the operator's servers and the organization providers) and checks every
connection against the case's own description: the application's plan, the requested tier, the
providers that exist (north's and south's, enabled or not, with a key or not) and the modes.
Seeded, so a failure is reproducible. Test values, not secrets."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

import pytest

from gateway.applications import Application
from gateway.egress import LocalDestinations
from gateway.modes import Mode, effective_mode, may_reach_hosted
from gateway.providers import ProviderConfig
from gateway.routing import Plan
from mock_network import Connection
from test_chat_routing import FIVE_XX, KEY, TEXT, TOOLS, refused, routing

SEED = 20261026
CASES = 400
TIERS = ("economy", "standard", "premium")
PORTS = {tier: 8081 + i for i, tier in enumerate(TIERS)}
OPERATORS = {tier: f"http://127.0.0.1:{port}/v1" for tier, port in PORTS.items()}
LOCAL = LocalDestinations.parse(",".join(f"127.0.0.1:{port}" for port in PORTS.values()))
MODES = tuple(Mode)
# local_only is the default and refuses every provider: less of it, so more requests reach one.
MODE_WEIGHTS = (1, 3, 3)


@dataclass(frozen=True)
class Case:
    application_id: str
    plan: tuple[str, ...]
    organization_mode: Mode
    application_mode: Mode
    providers: dict[str, ProviderConfig]  # by the address it resolves to
    keyed: frozenset[str]
    model: str
    stream: bool
    tools: bool


def _sent_model(connection: Connection) -> str | None:
    """The model named in the request body this connection carried; None when it carried
    none (the connection was refused). Its address is checked all the same."""
    _, _, body = bytes(connection.written).partition(b"\r\n\r\n")
    return str(json.loads(body)["model"]) if body else None


def _random_case(rng: random.Random, number: int) -> Case:
    plan = tuple(t for t in TIERS if rng.random() < 0.75) or (rng.choice(TIERS),)
    model = rng.choice(TIERS)
    organization_mode, application_mode = rng.choices(MODES, weights=MODE_WEIGHTS, k=2)
    # Most providers have the role the effective mode would use them in; the rest either.
    used = (
        "primary"
        if effective_mode(organization_mode, application_mode)
        is Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK
        else "fallback"
    )
    providers: dict[str, ProviderConfig] = {}
    keyed: set[str] = set()
    for i in range(rng.randint(0, 8)):
        provider_id = f"prv_{number:08x}{i:08x}"
        tools = rng.choice([None, True, False])
        config = ProviderConfig(
            provider_id,
            rng.choice(["north", "north", "south"]),
            "openai_compatible",
            f"https://{provider_id}.provider.test/v1",
            f"model-{provider_id}",
            # Half serve the requested tier, so that many are candidates; the rest any tier.
            model if rng.random() < 0.5 else rng.choice(TIERS),
            used if rng.random() < 0.7 else rng.choice(["primary", "fallback"]),
            rng.random() < 0.8,
            rng.random() < 0.9,
            None,
            None,
            {} if tools is None else {"supports_tools": {"value": tools, "source": "probed"}},
            None,
            0.0,
        )
        # Unique within the case, which is all the checks need (each case starts with none).
        providers[f"93.184.{number % 250}.{i + 1}"] = config
        if rng.random() < 0.85:
            keyed.add(provider_id)
    return Case(
        f"inv-{number}",
        plan,
        organization_mode,
        application_mode,
        providers,
        frozenset(keyed),
        model,
        rng.random() < 0.5,
        rng.random() < 0.3,
    )


async def test_no_request_ever_leaves_its_plan_its_tier_or_its_providers() -> None:
    rng = random.Random(SEED)
    seen = {"refused_by_plan": 0, "to_providers": 0, "to_operator": 0, "fallbacks": 0}
    async with routing(operators=OPERATORS, local=LOCAL) as r:
        store = r.world.keys.store
        for number in range(CASES):
            case = _random_case(rng, number)
            store.add_application(
                Application(
                    case.application_id,
                    f"Invariant {number}",
                    Plan("invariant", case.plan, 1.0, 10**12),
                    None,
                    None,
                    0.0,
                    "north",
                )
            )
            api_key = r.world.keys.issue(case.application_id, "main").key
            r.modes.set_organization_mode("north", case.organization_mode, 0.0)
            r.modes.set_application_mode(case.application_id, case.application_mode, 0.0)
            for address, config in case.providers.items():
                r.resolver.table[f"{config.id}.provider.test"] = [address]
                r.service.store.add(config)
                if config.id in case.keyed:
                    r.service.store.put_key(
                        config.id, r.service.vault.seal(KEY, config.binding), 0.0
                    )
            failures: list[Any] = [TEXT, FIVE_XX, refused()]
            r.org_net.responses = [rng.choice(failures) for _ in range(16)]
            # The operator fails two times in three, so many requests fall back.
            r.op_net.responses = [rng.choice(failures) for _ in range(4)]
            org_before, op_before = len(r.org_net.connections), len(r.op_net.connections)

            body: dict[str, Any] = {
                "model": case.model,
                "messages": [{"role": "user", "content": "Say hello."}],
                "stream": case.stream,
            }
            if case.tools:
                body["tools"] = TOOLS
            response = await r.client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )

            to_providers = r.org_net.connections[org_before:]
            to_operator = r.op_net.connections[op_before:]
            in_plan = case.model in case.plan
            hosted_allowed = may_reach_hosted(
                effective_mode(case.organization_mode, case.application_mode)
            )
            context = f"case {number}: {case}"
            if not in_plan:
                assert response.status_code == 403, context
                assert (to_providers, to_operator) == ([], []), context
                seen["refused_by_plan"] += 1
            for connection in to_providers:
                config = case.providers[connection.address]
                assert config.organization_id == "north", context
                assert config.serves_tier == case.model, context
                assert in_plan, context
                assert config.is_enabled, context
                assert config.id in case.keyed, context
                assert hosted_allowed, context
                assert _sent_model(connection) in (config.model, None), context
            for connection in to_operator:
                assert connection.port == PORTS[case.model], context
                assert in_plan, context
                assert _sent_model(connection) in (f"local-{case.model}", None), context
            provider = response.headers.get("x-provider")
            if provider is not None and provider != "gateway":
                answered = {c.id: c for c in case.providers.values()}[provider]
                assert (answered.organization_id, answered.serves_tier) == ("north", case.model)
            seen["to_providers"] += len(to_providers)
            seen["to_operator"] += len(to_operator)
            seen["fallbacks"] += "x-fallback-from" in response.headers
            # Each case is its own world of providers: the next one starts with none.
            for config in case.providers.values():
                r.service.store.delete(config.id)
        assert r.service.store.list_all() == []
    # Not vacuous: every kind of outcome happened, many times over.
    assert all(count >= 20 for count in seen.values()), seen


@pytest.mark.parametrize("model", TIERS)
def test_the_invariant_cases_cover_every_tier(model: str) -> None:
    """The generator itself: each tier is requested both inside and outside a plan, and has
    providers of north's and of south's serving it, in some case."""
    rng = random.Random(SEED)
    cases = [_random_case(rng, n) for n in range(CASES)]
    requested = [c for c in cases if c.model == model]
    assert any(model in c.plan for c in requested)
    assert any(model not in c.plan for c in requested)
    serving = [p for c in cases for p in c.providers.values() if p.serves_tier == model]
    assert {p.organization_id for p in serving} == {"north", "south"}
