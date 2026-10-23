"""Management requests on the running server, as docs/30 quotes.

MEASURED on the machine it runs on: real time, real CPU, real disk, through HTTP. Each run
starts the real server (``python -m gateway.serve``) on a throwaway database with a throwaway
pepper, logs in as an org_admin of the ``default`` organization, opens a step-up window, and
times from a client on the same machine, interleaved round by round:

- "healthz": no session at all, the floor of a request through this stack;
- "/me": a session check, no roles (what a console page asks first);
- "list users", "read application", "list keys": a management read, through ``requires``
  (the session, the roles read again, the resolver, ``authorize``) and the handler;
- "issue + revoke key": two management writes with step-up, timed together (a key is issued
  and revoked at once, so the application never reaches its limit of active keys).

Logins and step-up hash with argon2 and are not timed here (docs/24 and docs/27 time them).
The users, their passwords, the pepper and every key are made here and never printed.

    uv run python -m benchmarks.management_requests
    uv run python -m benchmarks.management_requests --rounds 500 --runs 3
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
from gateway.apikeys import ApiKeyService
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog
from gateway.auth import AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.management import ManagementService
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.passwords import (
    PRODUCTION,
    InlineHashRunner,
    PasswordHasher,
    Passwords,
    parameters_from,
)
from gateway.roles import RoleStore
from gateway.routing import load_plans
from gateway.users import AccountStore, Bootstrap

ADMIN = "benchadmin"
ORG = "default"
WARMUP_ROUNDS = 20
KINDS = ("healthz", "/me", "list users", "read application", "list keys", "issue + revoke key")


def _prepare(path: Path, pepper: bytes, environ: dict[str, str]) -> tuple[str, str]:
    """A platform_admin (the server refuses to start without one), an org_admin of
    ``default`` who has set its own password, and one application. Returns the org_admin's
    password and the application's id."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    hasher = PasswordHasher(parameters_from(environ, PRODUCTION))
    store = SqliteApplicationStore(connection)
    service = AccountService(
        AccountStore(connection),
        AuditLog(connection),
        Passwords(hasher, InlineHashRunner()),
        roles=RoleStore(connection),
        organizations=store,
        clock=WallClock(),
        epoch=SystemEpoch(),
        secret=secrets.token_bytes(32),
    )
    service.create_user("benchroot", secrets.token_urlsafe(18), ORG, platform_admin=True)
    password = secrets.token_urlsafe(18)
    user = service.create_user(ADMIN, password, ORG)
    # As if it had logged in once and chosen this password.
    service.store.set_password(
        user.id, hasher.hash(password), must_change=False, bootstrap=Bootstrap.NONE
    )
    service.grant_role(ADMIN, "org_admin", "organization", ORG)
    keys = ApiKeyService(store, pepper, SystemEpoch())
    operator = ManagementService(keys, load_plans(), AuditLog(connection), SystemEpoch())
    application = operator.create_application(ORG, "Bench", "pro")
    connection.close()
    return password, application.id


async def _timed(
    client: httpx.AsyncClient, clock: WallClock, method: str, url: str, expect: int, **kw: object
) -> tuple[float, httpx.Response]:
    started = clock.now_ms()
    response = await client.request(method, url, **kw)  # type: ignore[arg-type]
    elapsed = clock.now_ms() - started
    if response.status_code != expect:
        raise RuntimeError(f"{method} {url}: expected {expect}, got {response.status_code}")
    return elapsed, response


async def _measure(base: str, password: str, app_id: str, rounds: int) -> dict[str, list[float]]:
    clock = WallClock()
    results: dict[str, list[float]] = {kind: [] for kind in KINDS}
    admin = f"/v1/admin/orgs/{ORG}"
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        login = await client.post("/v1/auth/login", json={"username": ADMIN, "password": password})
        if login.status_code != 200:
            raise RuntimeError(f"login: {login.status_code}")
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        for round_ in range(WARMUP_ROUNDS + rounds):
            if round_ % 100 == 0:  # a window lasts 5 minutes; renew it well before
                step_up = await client.post(
                    "/v1/auth/step-up", json={"password": password}, headers=csrf
                )
                if step_up.status_code != 200:
                    raise RuntimeError(f"step-up: {step_up.status_code}")
            times = {
                "healthz": (await _timed(client, clock, "GET", "/healthz", 200))[0],
                "/me": (await _timed(client, clock, "GET", "/v1/auth/me", 200))[0],
                "list users": (await _timed(client, clock, "GET", f"{admin}/users", 200))[0],
                "read application": (
                    await _timed(client, clock, "GET", f"{admin}/applications/{app_id}", 200)
                )[0],
                "list keys": (
                    await _timed(client, clock, "GET", f"{admin}/applications/{app_id}/keys", 200)
                )[0],
            }
            issued_ms, issued = await _timed(
                client,
                clock,
                "POST",
                f"{admin}/applications/{app_id}/keys",
                201,
                json={"label": "bench"},
                headers=csrf,
            )
            key_id = issued.json()["key_id"]
            del issued  # the key is never kept or printed
            revoked_ms, _ = await _timed(
                client,
                clock,
                "POST",
                f"{admin}/applications/{app_id}/keys/{key_id}/revoke",
                200,
                headers=csrf,
            )
            times["issue + revoke key"] = issued_ms + revoked_ms
            if round_ >= WARMUP_ROUNDS:
                for kind, elapsed in times.items():
                    results[kind].append(elapsed)
    return results


def _run(rounds: int, environ: dict[str, str]) -> dict[str, list[float]]:
    # Windows releases a stopped server's files a moment after it exits; the folder is then
    # left for the system to clean rather than failing the run.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "gateway.db"
        pepper = secrets.token_urlsafe(32)
        password, app_id = _prepare(path, pepper.encode(), environ)
        port = _free_port()
        server_env = {**environ, "GATEWAY_DATABASE": str(path), "GATEWAY_KEY_PEPPER": pepper}
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
            return asyncio.run(_measure(base, password, app_id, rounds))
        finally:
            process.terminate()
            process.wait(timeout=30)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time management requests on the server.")
    parser.add_argument("--rounds", type=int, default=500)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.runs < 1:
        parser.error("--rounds and --runs must be at least 1")

    machine = describe_machine()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    for name in (
        "GATEWAY_ARGON2_MEMORY_KIB",
        "GATEWAY_ARGON2_TIME_COST",
        "GATEWAY_ARGON2_PARALLELISM",
    ):
        if name in os.environ:
            environ[name] = os.environ[name]
    print("MEASURED on this machine: real time, real CPU, real disk, over HTTP to the server.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(
        f"{args.runs} runs of {args.rounds} rounds each, after {WARMUP_ROUNDS} warm-up rounds;"
        " times in ms; percentiles: nearest rank"
    )
    p50s: dict[str, list[float]] = {kind: [] for kind in KINDS}
    for run in range(1, args.runs + 1):
        results = _run(args.rounds, environ)
        print(f"run {run}")
        for kind in KINDS:
            times = results[kind]
            p50s[kind].append(percentile(times, 50))
            print(
                f"  {kind:<20} p50 {percentile(times, 50):>7.2f}  p90 {percentile(times, 90):>7.2f}"
                f"  mean {sum(times) / len(times):>7.2f}  max {max(times):>7.2f}"
            )
    print("p50 across runs (min to max)")
    for kind in KINDS:
        print(f"  {kind:<20} {min(p50s[kind]):>7.2f} to {max(p50s[kind]):>7.2f}")


if __name__ == "__main__":
    main()
