"""What one webhook delivery costs in process (docs/46, the A4 plan's section 17).

MEASURED on the machine it runs on: real time, in process. Each round queues one notification
for one channel (not timed), then times one delivery through the delivery worker: the claim,
the read of the channel and its sealed secrets, the decryption, the request built and signed,
the attempt through ``webhook_client`` (the guard, the port rule, the stage bounds) onto a
scripted network that answers 200 at once, and the outcome with the channel's health. The
network is scripted, so this is the gateway's own cost, not a receiver's: no number here is a
webhook's latency.

On SQLite (a file) and, with ``GATEWAY_POSTGRES_URL``, on Postgres (a schema of its own,
dropped afterwards), each in three passes.

    uv run python -m benchmarks.alert_delivery
    GATEWAY_POSTGRES_URL=postgresql://... uv run python -m benchmarks.alert_delivery
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import random
import secrets
import tempfile
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpcore

from benchmarks.calibrate import describe_machine
from gateway.alerts.caps import AlertCaps
from gateway.alerts.channels import ChannelService
from gateway.alerts.delivery import DeliveryWorker
from gateway.alerts.notices import Notices
from gateway.alerts.payload import AlertPayload
from gateway.applications import application_from_template
from gateway.audit import AuditLog
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.egress import provider_tls_context, webhook_client
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.permissions import Actor
from gateway.postgres import PostgresDatabase, apply_schema, postgres_state
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.state import StateBackend, sqlite_state
from gateway.vault import Vault

WARMUP = 20
PASSES = 3
ORG, APP = "bench", "bench-app"
HOST, ADDRESS = "hooks.bench.example", "93.184.216.34"
ANSWER = b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}"


class Network(httpcore.AsyncNetworkBackend):
    """Answers every connection with a 200 at once."""

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return httpcore.AsyncMockStream([ANSWER])

    async def sleep(self, seconds: float) -> None:
        return None


async def _resolve(host: str, port: int) -> list[str]:
    return [ADDRESS]


class _Marker:
    def changed(self) -> None:
        return None


async def _pass(state: StateBackend, rounds: int) -> list[float]:
    clock, epoch = WallClock(), SystemEpoch()
    vault = Vault(secrets.token_bytes(32))
    notices = Notices(
        state.channels,
        state.outbox,
        state.applications,
        epoch,
        secrets.token_bytes,
        caps=AlertCaps(per_rule=60, per_organization=600),
        store_clock=state.clock,
    )
    service = ChannelService(
        state.channels,
        vault,
        AuditLog(state.audit),
        epoch,
        secrets.token_bytes,
        notices=notices,
        store_clock=state.clock,
    )
    made = await service.create(
        ORG,
        name=f"Bench {secrets.token_hex(4)}",
        url=f"https://{HOST}/hook/bench",
        is_default=True,
        channel_type="webhook",
        actor=Actor("usr_00000000000000b1", ORG, ()),
        marker=_Marker(),
    )
    channel = made.channel
    tls = provider_tls_context()  # once, as the server does
    network = Network()
    worker = DeliveryWorker(
        state.outbox,
        state.channels,
        vault,
        Redactor(),
        store_clock=state.clock,
        clock=clock,
        epoch=epoch,
        client=lambda: webhook_client(
            _resolve, clock, frozenset({443}), inner=network, ssl_context=tls
        ),
        rng=random.Random(1),
        random_bytes=secrets.token_bytes,
    )
    times: list[float] = []
    for i in range(WARMUP + rounds):
        payload = AlertPayload("ntf_" + secrets.token_hex(8), "test", epoch.epoch_ms(), ORG)
        async with state.outbox.delivery_transaction():
            await notices.queue(payload, [channel], counted=False)
        started = clock.now_ms()
        claimed = await worker.step()
        await worker.drain()
        elapsed = clock.now_ms() - started
        if claimed != 1:
            raise RuntimeError(f"claimed {claimed}, not 1")
        if i >= WARMUP:
            times.append(elapsed)
    delivered = [
        d
        for d in await state.outbox.channel_deliveries(channel.id, 10_000)
        if d.state.value == "delivered"
    ]
    if len(delivered) != WARMUP + rounds:
        raise RuntimeError("not every delivery was delivered")
    return times


async def _seed(state: StateBackend) -> None:
    await state.applications.add_organization(
        new_organization(ORG, "Bench", "business", created_at_ms=1.0)
    )
    made = application_from_template(
        "x", "Bench", load_plans(), "pro", organization_id=ORG, created_at_ms=1.0
    )
    await state.applications.add_application(dataclasses.replace(made, id=APP))


@asynccontextmanager
async def _sqlite() -> AsyncIterator[StateBackend]:
    with tempfile.TemporaryDirectory() as folder:
        connection = open_database(Path(folder) / "bench.db")
        try:
            MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
            state = sqlite_state(connection, SystemEpoch())
            await _seed(state)
            yield state
        finally:
            connection.close()


@asynccontextmanager
async def _postgres(url: str, env: Mapping[str, str]) -> AsyncIterator[StateBackend]:
    schema = f"gwbench_{secrets.token_hex(8)}"
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        db = await PostgresDatabase.connect(url, WallClock(), env=env, schema=schema)
        try:
            await apply_schema(db)
            state = postgres_state(db)
            await _seed(state)
            async with db.evaluation():  # the worker's pool, as in the server
                yield state
        finally:
            await db.aclose()
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def run(rounds: int, url: str | None, env: Mapping[str, str]) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for n in range(PASSES):
        async with _sqlite() as state:
            found[f"sqlite pass {n + 1}"] = await _pass(state, rounds)
        if url is not None:
            async with _postgres(url, env) as state:
                found[f"postgres pass {n + 1}"] = await _pass(state, rounds)
    return found


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time one webhook delivery in process.")
    parser.add_argument("--rounds", type=int, default=300)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    url = os.environ.get("GATEWAY_POSTGRES_URL")
    machine = describe_machine()
    print("MEASURED on this machine: real time, in process, a scripted network (no receiver).")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"rounds per pass: {args.rounds} (warm-up {WARMUP}); nearest rank")
    if url is None:
        print("GATEWAY_POSTGRES_URL not set: SQLite only")
    print()
    print(f"{'store, pass':<18} {'n':>5} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for name, times in asyncio.run(run(args.rounds, url, os.environ)).items():
        print(
            f"{name:<18} {len(times):>5} {percentile(times, 50):>9.3f} "
            f"{percentile(times, 99):>9.3f} {max(times):>9.3f}"
        )


if __name__ == "__main__":
    main()
