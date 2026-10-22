"""Logins on the running server, as docs/24 and docs/27 quote.

MEASURED on the machine it runs on: real time, real CPU, real disk, through HTTP. It starts
the real server (``python -m gateway.serve``) on a throwaway database with a throwaway pepper,
at the argon2 setting the server would use (the default unless ``GATEWAY_ARGON2_*`` is set),
and times, from a client on the same machine:

- "one login": sequential successful logins, one at a time;
- "8 at once": 8 logins sent together, each timed from sending to the answer (the server
  hashes 2 at a time, so later ones wait);
- "failed, known" and "failed, unknown": refused logins for a username that exists (wrong
  password) and for one that does not, interleaved, enough of each to compare. Each known
  user fails at most 4 times, so none is locked and every one takes the same path.

The users, their passwords and the pepper are made here and never printed.

    uv run python -m benchmarks.login
    uv run python -m benchmarks.login --failures 300 --logins 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx

from benchmarks.calibrate import describe_machine
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog
from gateway.auth import AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.passwords import (
    PRODUCTION,
    InlineHashRunner,
    PasswordHasher,
    Passwords,
    parameters_from,
)
from gateway.users import AccountStore

FAILURES_PER_KNOWN_USER = 4  # below the lockout's 5


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _make_users(path: Path, count: int, environ: dict[str, str]) -> dict[str, str]:
    """``count`` users in the default organization; returns username -> password."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    hasher = PasswordHasher(parameters_from(environ, PRODUCTION))
    service = AccountService(
        AccountStore(connection),
        AuditLog(connection),
        Passwords(hasher, InlineHashRunner()),
        organizations=SqliteApplicationStore(connection),
        clock=WallClock(),
        epoch=SystemEpoch(),
        limiter_secret=secrets.token_bytes(32),
    )
    users = {}
    for i in range(count):
        name = f"bench{i:03d}"
        users[name] = secrets.token_urlsafe(18)
        service.create_user(name, users[name], "default", platform_admin=i == 0)
    connection.close()
    return users


def _audit_rows(path: Path) -> int:
    connection = open_database(path)
    try:
        return int(connection.execute("SELECT count(*) FROM audit_log").fetchone()[0])
    finally:
        connection.close()


async def _timed_login(
    client: httpx.AsyncClient, clock: WallClock, user: str, pw: str, *, expect: int = 200
) -> float:
    """The time from sending a login to its answer; the answer must be ``expect``."""
    started = clock.now_ms()
    response = await client.post("/v1/auth/login", json={"username": user, "password": pw})
    elapsed = clock.now_ms() - started
    if response.status_code != expect:
        raise RuntimeError(f"expected {expect}, got {response.status_code}")
    return elapsed


async def _measure(
    base: str, users: dict[str, str], logins: int, failures: int
) -> dict[str, list[float]]:
    clock = WallClock()
    names = list(users)
    results: dict[str, list[float]] = {"one login": [], "8 at once": [], "known": [], "unknown": []}
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        first = names[0]
        for _ in range(2):  # warm-up
            await _timed_login(client, clock, first, users[first])
        for _ in range(logins):
            results["one login"].append(await _timed_login(client, clock, first, users[first]))
        results["8 at once"] = list(
            await asyncio.gather(*(_timed_login(client, clock, n, users[n]) for n in names[1:9]))
        )
        known = [n for n in names[9:] for _ in range(FAILURES_PER_KNOWN_USER)][:failures]
        for i, name in enumerate(known):
            # Interleaved, so drift in the machine affects both sides alike.
            results["known"].append(
                await _timed_login(client, clock, name, "wrong password", expect=401)
            )
            unknown = f"nobody{i:05d}{secrets.token_hex(2)}"
            results["unknown"].append(
                await _timed_login(client, clock, unknown, "wrong password", expect=401)
            )
    return results


def _summary(times: list[float]) -> str:
    return (
        f"n={len(times):>4}  p50 {percentile(times, 50):>8.1f}  p90 {percentile(times, 90):>8.1f}"
        f"  mean {sum(times) / len(times):>8.2f}  max {max(times):>8.1f}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time logins on the running server.")
    parser.add_argument("--logins", type=int, default=20)
    parser.add_argument("--failures", type=int, default=240, help="of each kind")
    args = parser.parse_args(argv)
    if args.logins < 1 or args.failures < 1:
        parser.error("--logins and --failures must be at least 1")

    machine = describe_machine()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    for name in (
        "GATEWAY_ARGON2_MEMORY_KIB",
        "GATEWAY_ARGON2_TIME_COST",
        "GATEWAY_ARGON2_PARALLELISM",
    ):
        if name in os.environ:
            environ[name] = os.environ[name]
    setting = parameters_from(environ, PRODUCTION)
    print("MEASURED on this machine: real time, real CPU, real disk, over HTTP to the server.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(
        f"argon2id m={setting.memory_cost_kib} KiB t={setting.time_cost} p={setting.parallelism};"
        " 2 hashing threads; times in ms; percentiles: nearest rank"
    )
    known_users = -(-args.failures // FAILURES_PER_KNOWN_USER)
    # Windows releases a stopped server's files a moment after it exits; the folder is then
    # left for the system to clean rather than failing the run.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "gateway.db"
        users = _make_users(path, 9 + known_users, environ)
        port = _free_port()
        server_env = {
            **environ,
            "GATEWAY_DATABASE": str(path),
            "GATEWAY_KEY_PEPPER": secrets.token_urlsafe(32),
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
            before = _audit_rows(path)
            results = asyncio.run(_measure(base, users, args.logins, args.failures))
            after = _audit_rows(path)
        finally:
            process.terminate()
            process.wait(timeout=30)
    print(f"one login        {_summary(results['one login'])}")
    burst = results["8 at once"]
    print(f"8 at once        {_summary(burst)}  (the last answered after {max(burst):.1f})")
    print(f"failed, known    {_summary(results['known'])}")
    print(f"failed, unknown  {_summary(results['unknown'])}")
    difference = percentile(results["known"], 50) - percentile(results["unknown"], 50)
    mean_difference = (sum(results["known"]) - sum(results["unknown"])) / len(results["known"])
    print(f"known minus unknown: p50 {difference:+.2f} ms, mean {mean_difference:+.2f} ms")
    print(f"audit rows written during the run: {after - before}")


if __name__ == "__main__":
    main()
