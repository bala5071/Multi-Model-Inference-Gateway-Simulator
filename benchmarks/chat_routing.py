"""What routing a chat request to an organization's provider adds, as docs/33 quotes.

MEASURED on the machine it runs on: real time, real CPU, in this process. No sockets: the
providers are a scripted network behind the real egress guard (``httpcore``'s mock streams),
so what is timed is the gateway's own work, never a provider's latency. Each request goes
through the whole app (``httpx.ASGITransport``): key check, plan, rate limit, budget, router,
adapter, ledger.

- "operator tier": the economy tier served by the operator's server (an OpenAI-compatible
  adapter at a local destination), ``local_only``: the path of PR 1, now through the router.
- "org provider": the same answer from an organization's provider,
  ``hosted_primary_with_local_fallback``: the router's candidates and breaker, the key
  decrypted for the call, a fresh guarded client, the guard's checks, the provider's prices.
- "fallback": the first provider refuses the connection at once and the second answers.
- "tls context": building one TLS context (``httpx.create_ssl_context()``), what each request
  to an organization provider paid, twice, before the server shared one (docs/33).

The provider keys and the pepper are made here and never printed.

    uv run python -m benchmarks.chat_routing
    uv run python -m benchmarks.chat_routing --rounds 200 --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from collections.abc import Iterable, Sequence
from typing import Any

import httpcore
import httpx

from benchmarks.calibrate import describe_machine
from gateway.adapters import OpenAICompatibleAdapter
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.audit import AuditLog, SqliteAuditStore
from gateway.backends import Backend
from gateway.clock import SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.egress import LocalDestinations, operator_client, organization_client
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.modes import InMemoryModeStore, Mode
from gateway.providers import ProviderConfig, ProviderService, SqliteProviderStore
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.vault import Vault

WARMUP = 20
ORG, APP = "default", "acme"
OPERATOR = "http://127.0.0.1:8081/v1"
ADDRESSES = {"a.provider.bench": "93.184.216.34", "b.provider.bench": "93.184.216.35"}


def _answer() -> bytes:
    """An OpenAI-compatible streamed answer, as raw HTTP."""
    events = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 1}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    head = "HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/event-stream\r\n\r\n"
    return (head + body).encode()


ANSWER = _answer()


class Network(httpcore.AsyncNetworkBackend):
    """Answers every connection with ``ANSWER``, except to addresses in ``refusing``."""

    def __init__(self, refusing: Iterable[str] = ()) -> None:
        self.refusing = set(refusing)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host in self.refusing:
            raise httpcore.ConnectError("refused")
        return httpcore.AsyncMockStream([ANSWER])

    async def sleep(self, seconds: float) -> None:
        return None  # httpcore sleeps only between retries, and none are made


async def _resolve(host: str, port: int) -> list[str]:
    return [ADDRESSES[host]]


async def _case(name: str, rounds: int) -> list[float]:
    clock = WallClock()
    epoch = SystemEpoch()
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    templates = load_plans()
    await seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
    keys = ApiKeyService(store, secrets.token_bytes(32), epoch)
    api_key = (await keys.issue(APP, "bench")).key
    tls = httpx.create_ssl_context()  # once, as the server does
    providers = ProviderService(
        SqliteProviderStore(connection),
        Vault(secrets.token_bytes(32)),
        AuditLog(SqliteAuditStore(connection)),
        clock,
        epoch,
        Redactor(),
        lambda: organization_client(
            _resolve,
            inner=Network(refusing={"93.184.216.34"} if name == "fallback" else ()),
            ssl_context=tls,
        ),
    )
    modes = InMemoryModeStore()
    if name != "operator tier":
        await modes.set_organization_mode(ORG, Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK, 0.0)
        await modes.set_application_mode(APP, Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK, 0.0)
        for provider_id, host in (("prv_00000000000000a1", "a"), ("prv_00000000000000b2", "b")):
            config = ProviderConfig(
                provider_id, ORG, "openai_compatible", f"https://{host}.provider.bench/v1",
                "model-x", "economy", "primary", True, True, 1.0, 2.0, {}, None, 0.0,
            )  # fmt: skip
            await providers.store.add(config)
            key = secrets.token_urlsafe(32)
            await providers.store.put_key(
                provider_id, providers.vault.seal(key, config.binding), 0.0
            )
    local = LocalDestinations.parse("127.0.0.1:8081")
    adapters: dict[str, Backend] = {
        "economy": OpenAICompatibleAdapter(
            name="economy",
            base_url=OPERATOR,
            model="local-model",
            client=operator_client(_resolve, local, inner=Network()),
        )
    }
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, templates),
        epoch=epoch,
        providers=providers,
        chat_adapters=adapters,
        modes=modes,
        local_destinations=local,
    )
    body = {"model": "economy", "messages": [{"role": "user", "content": "Say hello."}]}
    headers = {"Authorization": f"Bearer {api_key}"}
    expected = "prv_00000000000000a1" if name == "org provider" else None
    times: list[float] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        for i in range(WARMUP + rounds):
            started = clock.now_ms()
            response = await client.post("/v1/chat/completions", json=body, headers=headers)
            elapsed = clock.now_ms() - started
            if response.status_code != 200:
                raise RuntimeError(f"{name}: {response.status_code}")
            provider = response.headers["x-provider"]
            if expected is not None and provider != expected:
                raise RuntimeError(f"{name}: answered by {provider}")
            if name == "fallback" and response.headers.get("x-fallback-reason") != "connect_error":
                raise RuntimeError(f"fallback: no fallback happened: {dict(response.headers)}")
            if i >= WARMUP:
                times.append(elapsed)
            # Untimed: keep the refusing provider's breaker closed, so every request pays for
            # a real first attempt rather than a skip once the breaker opens.
            await app.state.chat_router.breakers.success(ORG, "prv_00000000000000a1")
    await registry.aclose()
    connection.close()
    return times


CASES = ("operator tier", "org provider", "fallback")


def _tls(rounds: int) -> list[float]:
    clock = WallClock()
    times = []
    for i in range(WARMUP + rounds):
        started = clock.now_ms()
        httpx.create_ssl_context()
        if i >= WARMUP:
            times.append(clock.now_ms() - started)
    return times


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time chat routing with mock providers.")
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.runs < 1:
        parser.error("--rounds and --runs must be at least 1")
    machine = describe_machine()
    print("MEASURED on this machine: real time, real CPU, in process; scripted providers.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"{args.runs} runs of {args.rounds} requests after {WARMUP} warm-up; ms; nearest rank")
    p50s: dict[str, list[float]] = {}
    for run in range(1, args.runs + 1):
        print(f"run {run}")
        for name in (*CASES, "tls context"):
            times = (
                _tls(args.rounds)
                if name == "tls context"
                else asyncio.run(_case(name, args.rounds))
            )
            p50s.setdefault(name, []).append(percentile(times, 50))
            print(
                f"  {name:<14} p50 {percentile(times, 50):>8.3f}  p90 {percentile(times, 90):>8.3f}"
                f"  max {max(times):>8.3f}"
            )
    print("p50 across runs (min to max)")
    for name, values in p50s.items():
        print(f"  {name:<14} {min(values):>8.3f} to {max(values):>8.3f}")


if __name__ == "__main__":
    main()
