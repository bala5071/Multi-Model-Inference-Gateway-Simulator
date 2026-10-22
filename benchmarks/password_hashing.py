"""What argon2id costs on this machine, as docs/24-password-hashing.md quotes.

MEASURED on the machine it runs on: real time, real CPU. At the production parameters
(``gateway.passwords.PRODUCTION``: argon2id, 19 MiB, 2 iterations, 1 lane):

- "hash" and "verify": one at a time, in this thread;
- "login, N at once": N verifies submitted together to the server's ``ThreadHashRunner``
  (2 threads): each one's time from submission to result, so queueing behind the pool shows;
- "loop stall": while those run, the longest gap seen by a task that wakes every 1 ms on the
  event loop. Small means the loop kept serving; a hash on the loop itself would stall it
  for a whole hash.

    uv run python -m benchmarks.password_hashing
    uv run python -m benchmarks.password_hashing --repeats 50 --together 16
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.clock import WallClock
from gateway.metrics import percentile
from gateway.passwords import PRODUCTION, PasswordHasher, Passwords, ThreadHashRunner

PASSWORD = "a benchmark passphrase, not anyone's password"


def _summary(name: str, times: list[float]) -> WriteTimes:
    return WriteTimes(
        store=name,
        writes=len(times),
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


def _time(clock: WallClock, work: Callable[[], object], repeats: int) -> list[float]:
    work()  # warm-up
    times = []
    for _ in range(repeats):
        started = clock.now_ms()
        work()
        times.append(clock.now_ms() - started)
    return times


async def _together(clock: WallClock, stored: str, count: int) -> tuple[list[float], float]:
    runner = ThreadHashRunner(max_workers=2)
    passwords = Passwords(PasswordHasher(PRODUCTION), runner)
    gaps: list[float] = []
    running = True

    async def ticker() -> None:
        last = clock.now_ms()
        while running:
            await clock.sleep_ms(1.0)
            now = clock.now_ms()
            gaps.append(now - last)
            last = now

    async def one() -> float:
        started = clock.now_ms()
        assert await passwords.verify(stored, PASSWORD)
        return clock.now_ms() - started

    task = asyncio.create_task(ticker())
    try:
        latencies = list(await asyncio.gather(*(one() for _ in range(count))))
    finally:
        running = False
        await task
        runner.close()
    return latencies, max(gaps) if gaps else 0.0


def run(repeats: int, together: int) -> tuple[list[WriteTimes], float]:
    clock = WallClock()
    hasher = PasswordHasher(PRODUCTION)
    stored = hasher.hash(PASSWORD)
    results = [
        _summary("hash", _time(clock, lambda: hasher.hash(PASSWORD), repeats)),
        _summary("verify", _time(clock, lambda: hasher.verify(stored, PASSWORD), repeats)),
    ]
    latencies, stall = asyncio.run(_together(clock, stored, together))
    results.append(_summary(f"login, {together} at once", latencies))
    return results, stall


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time argon2id at the production parameters.")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--together", type=int, default=8)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.together < 1:
        parser.error("--repeats and --together must be at least 1")

    machine = describe_machine()
    p = PRODUCTION
    print("MEASURED on this machine: real time, real CPU.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(
        f"argon2id m={p.memory_cost_kib} KiB, t={p.time_cost}, p={p.parallelism}; "
        "thread pool of 2 for logins; percentiles: nearest rank"
    )
    print()
    results, stall = run(args.repeats, args.together)
    print(f"{'what':<20} {'n':>4} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in results:
        print(
            f"{r.store:<20} {r.writes:>4} {r.mean_ms:>9.1f} {r.p50_ms:>9.1f} "
            f"{r.p99_ms:>9.1f} {r.max_ms:>9.1f}"
        )
    print(f"\nlongest event-loop stall during the logins: {stall:.1f} ms")


if __name__ == "__main__":
    main()
