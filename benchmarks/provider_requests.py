"""What the vault, the egress guard and the provider endpoints cost, as docs/32 quotes.

MEASURED on the machine it runs on: real time, real CPU.

- "vault open": decrypting one sealed provider key (AES-256-GCM), in this process.
- "egress check": the guard's resolve-and-check for one connection, with the system resolver,
  for the name ``localhost`` (answered by the system, no network traffic), which the guard then
  refuses: the cost of the check that runs before every connection to an organization's
  provider. Nothing is sent anywhere.
- On the running server (``python -m gateway.serve``, a throwaway database, pepper, vault key and
  admin), from a client on the same machine: "list providers" (``GET .../providers``),
  "status" (``GET .../providers/status``), and "set key" (``PUT .../providers/{id}/key``, a
  consume-on-use change: each is preceded by a step-up, which is not timed).

The key, the users' passwords, the pepper and the vault key are made here and never printed.

    uv run python -m benchmarks.provider_requests
    uv run python -m benchmarks.provider_requests --rounds 200 --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx

from benchmarks.calibrate import describe_machine
from benchmarks.login import _free_port
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog
from gateway.auth import AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.egress import EgressRefused, GuardedBackend, system_resolver
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.passwords import CHEAP, InlineHashRunner, PasswordHasher, Passwords
from gateway.roles import RoleStore
from gateway.users import AccountStore, Bootstrap
from gateway.vault import Binding, Vault

ADMIN = "benchadmin"
ORG = "default"
WARMUP = 20
BINDING = Binding(ORG, "prv_00000000000000aa", "openai_compatible", "https://api.example.com/v1")


def _in_process(rounds: int) -> dict[str, list[float]]:
    clock = WallClock()
    vault = Vault(secrets.token_bytes(32))
    sealed = vault.seal(secrets.token_urlsafe(40), BINDING)
    guard = GuardedBackend(system_resolver)
    times: dict[str, list[float]] = {"vault open": [], "egress check": []}

    async def check() -> None:
        try:
            await guard.connect_tcp("localhost", 443)
        except EgressRefused:
            return
        raise RuntimeError("the guard let localhost through")

    async def run() -> None:
        for i in range(WARMUP + rounds):
            started = clock.now_ms()
            vault.open(sealed, BINDING)
            opened = clock.now_ms() - started
            started = clock.now_ms()
            await check()
            checked = clock.now_ms() - started
            if i >= WARMUP:
                times["vault open"].append(opened)
                times["egress check"].append(checked)

    asyncio.run(run())
    return times


def _prepare(path: Path) -> str:
    """A platform_admin, and an org_admin of ``default`` with a known password (cheap argon2:
    logins and step-up are not what is timed)."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    hasher = PasswordHasher(CHEAP)
    service = AccountService(
        AccountStore(connection),
        AuditLog(connection),
        Passwords(hasher, InlineHashRunner()),
        roles=RoleStore(connection),
        organizations=SqliteApplicationStore(connection),
        clock=WallClock(),
        epoch=SystemEpoch(),
        secret=secrets.token_bytes(32),
    )
    service.create_user("benchroot", secrets.token_urlsafe(18), ORG, platform_admin=True)
    password = secrets.token_urlsafe(18)
    user = service.create_user(ADMIN, password, ORG)
    service.store.set_password(
        user.id, hasher.hash(password), must_change=False, bootstrap=Bootstrap.NONE
    )
    service.grant_role(ADMIN, "org_admin", "organization", ORG)
    connection.close()
    return password


async def _on_server(base: str, password: str, rounds: int) -> dict[str, list[float]]:
    clock = WallClock()
    times: dict[str, list[float]] = {"list providers": [], "status": [], "set key": []}
    admin = f"/v1/admin/orgs/{ORG}/providers"
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        login = await client.post("/v1/auth/login", json={"username": ADMIN, "password": password})
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}

        async def step_up() -> None:
            response = await client.post(
                "/v1/auth/step-up", json={"password": password}, headers=csrf
            )
            if response.status_code != 200:
                raise RuntimeError(f"step-up: {response.status_code}")

        await step_up()
        made = await client.post(
            admin,
            json={
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "model": "bench",
                "serves_tier": "standard",
            },
            headers=csrf,
        )
        provider_id = made.json()["provider_id"]
        for i in range(WARMUP + rounds):
            round_times = {}
            for name, url in (("list providers", admin), ("status", f"{admin}/status")):
                started = clock.now_ms()
                response = await client.get(url)
                round_times[name] = clock.now_ms() - started
                if response.status_code != 200:
                    raise RuntimeError(f"{name}: {response.status_code}")
            await step_up()  # consume-on-use: each key change needs a fresh window; not timed
            started = clock.now_ms()
            response = await client.put(
                f"{admin}/{provider_id}/key", json={"key": secrets.token_urlsafe(40)}, headers=csrf
            )
            round_times["set key"] = clock.now_ms() - started
            if response.status_code != 200:
                raise RuntimeError(f"set key: {response.status_code}")
            if i >= WARMUP:
                for name, value in round_times.items():
                    times[name].append(value)
    return times


def _server_run(rounds: int, environ: dict[str, str]) -> dict[str, list[float]]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "gateway.db"
        password = _prepare(path)
        port = _free_port()
        server_env = {
            **environ,
            "GATEWAY_DATABASE": str(path),
            "GATEWAY_KEY_PEPPER": secrets.token_urlsafe(32),
            "GATEWAY_VAULT_KEY": secrets.token_urlsafe(32),
            "GATEWAY_ARGON2_MEMORY_KIB": str(CHEAP.memory_cost_kib),
            "GATEWAY_ARGON2_TIME_COST": str(CHEAP.time_cost),
            "GATEWAY_ARGON2_PARALLELISM": str(CHEAP.parallelism),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "gateway.serve", "--port", str(port), "--log-level", "warning"],
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            pause = threading.Event()
            for _ in range(300):
                try:
                    if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                        break
                except httpx.TransportError:
                    pause.wait(0.1)
            else:
                raise RuntimeError("the server did not start")
            return asyncio.run(_on_server(base, password, rounds))
        finally:
            process.terminate()
            process.wait(timeout=30)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time the vault, the guard and the endpoints.")
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.runs < 1:
        parser.error("--rounds and --runs must be at least 1")
    machine = describe_machine()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    print("MEASURED on this machine: real time, real CPU; the server over HTTP on this machine.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"{args.runs} runs of {args.rounds} rounds after {WARMUP} warm-up; ms; nearest rank")
    p50s: dict[str, list[float]] = {}
    for run in range(1, args.runs + 1):
        results = {**_in_process(args.rounds), **_server_run(args.rounds, environ)}
        print(f"run {run}")
        for name, times in results.items():
            p50s.setdefault(name, []).append(percentile(times, 50))
            print(
                f"  {name:<16} p50 {percentile(times, 50):>8.3f}  p90 {percentile(times, 90):>8.3f}"
                f"  max {max(times):>8.3f}"
            )
    print("p50 across runs (min to max)")
    for name, values in p50s.items():
        print(f"  {name:<16} {min(values):>8.3f} to {max(values):>8.3f}")


if __name__ == "__main__":
    main()
