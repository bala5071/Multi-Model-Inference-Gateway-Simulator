"""What argon2id costs on this machine, at several settings, as docs/24 quotes.

MEASURED on the machine it runs on: real time, real CPU, real memory. For each setting
(memory, iterations, lanes), in a process of its own:

- "hash": one at a time, in this thread;
- "login, N at once": N verifies submitted together to the server's ``ThreadHashRunner``
  (2 threads), each timed from submission to result, so queueing behind the pool shows;
- "peak memory": the process's peak memory above what it used before any hashing, which
  is reached while two hashes run at once in the burst (Windows: peak working set; Linux
  and macOS: max RSS);
- "loop stall": the longest gap seen during the burst by a task waking every 1 ms.

    uv run python -m benchmarks.password_hashing
    uv run python -m benchmarks.password_hashing --setting 19456,2,1 --setting 65536,3,1

A setting is ``memory_kib,iterations,lanes``.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

from benchmarks.calibrate import describe_machine
from gateway.clock import WallClock
from gateway.metrics import percentile
from gateway.passwords import (
    PRODUCTION,
    Argon2Parameters,
    PasswordHasher,
    Passwords,
    ThreadHashRunner,
)

PASSWORD = "a benchmark passphrase, not anyone's password"
# The previous default (OWASP's first), OWASP's alternative at the same strength, the current
# default (RFC 9106's second, one lane) and a stronger one.
DEFAULT_SETTINGS = ("19456,2,1", "47104,1,1", "65536,3,1", "131072,3,1")


def parse_setting(text: str) -> Argon2Parameters:
    memory, time_cost, lanes = (int(part) for part in text.split(","))
    return Argon2Parameters(time_cost=time_cost, memory_cost_kib=memory, parallelism=lanes)


def peak_memory_bytes() -> int:
    """This process's peak memory so far."""
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = (
                ("cb", ctypes.c_ulong),
                ("page_fault_count", ctypes.c_ulong),
                ("peak_working_set", ctypes.c_size_t),
                ("rest", ctypes.c_size_t * 7),
            )

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        windll: Any = getattr(ctypes, "windll")  # noqa: B009
        # Declared types: the process handle is a pointer-sized -1, which an undeclared call
        # would pass as a 32-bit int and the API would reject.
        current = windll.kernel32.GetCurrentProcess
        current.restype = ctypes.c_void_p
        info = windll.psapi.GetProcessMemoryInfo
        info.argtypes = (ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong)
        info.restype = ctypes.c_int
        if not info(current(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.peak_working_set)
    else:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage) if sys.platform == "darwin" else int(usage) * 1024


def _stats(times: list[float]) -> dict[str, float]:
    return {
        "mean": sum(times) / len(times),
        "p50": percentile(times, 50),
        "p99": percentile(times, 99),
        "max": max(times),
    }


def _time(clock: WallClock, work: Callable[[], object], repeats: int) -> list[float]:
    work()  # warm-up
    times = []
    for _ in range(repeats):
        started = clock.now_ms()
        work()
        times.append(clock.now_ms() - started)
    return times


async def _burst(
    clock: WallClock, hasher: PasswordHasher, stored: str, count: int
) -> tuple[list[float], float]:
    runner = ThreadHashRunner(max_workers=2)
    passwords = Passwords(hasher, runner)
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


def measure_one(parameters: Argon2Parameters, repeats: int, together: int) -> dict[str, Any]:
    """Everything for one setting; run in a fresh process so the memory peak is its own."""
    clock = WallClock()
    hasher = PasswordHasher(parameters)
    before = peak_memory_bytes()
    stored = hasher.hash(PASSWORD)
    hash_times = _time(clock, lambda: hasher.hash(PASSWORD), repeats)
    latencies, stall = asyncio.run(_burst(clock, hasher, stored, together))
    return {
        "memory_kib": parameters.memory_cost_kib,
        "time_cost": parameters.time_cost,
        "parallelism": parameters.parallelism,
        "hash": _stats(hash_times),
        "burst": _stats(latencies),
        "peak_memory_mib": (peak_memory_bytes() - before) / 2**20,
        "stall_ms": stall,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time argon2id at several settings.")
    parser.add_argument("--setting", action="append", help="memory_kib,iterations,lanes")
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--together", type=int, default=8)
    parser.add_argument("--one", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.together < 1:
        parser.error("--repeats and --together must be at least 1")

    if args.one:  # the child process: one setting, one JSON line
        print(json.dumps(measure_one(parse_setting(args.one), args.repeats, args.together)))
        return

    machine = describe_machine()
    print("MEASURED on this machine: real time, real CPU, real memory.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    current = f"{PRODUCTION.memory_cost_kib},{PRODUCTION.time_cost},{PRODUCTION.parallelism}"
    print(
        f"argon2id; current setting {current}; {args.repeats} hashes per setting; "
        f"{args.together} logins at once through 2 threads; percentiles: nearest rank"
    )
    print()
    print(
        f"{'memory MiB':>10} {'t':>2} {'p':>2} | {'hash p50':>8} {'hash max':>8} | "
        f"{'burst p50':>9} {'burst max':>9} | {'peak MiB':>8} | {'stall':>6}"
    )
    for setting in args.setting or DEFAULT_SETTINGS:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.password_hashing",
                "--one",
                setting,
                "--repeats",
                str(args.repeats),
                "--together",
                str(args.together),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        r = json.loads(done.stdout.strip().splitlines()[-1])
        print(
            f"{r['memory_kib'] / 1024:>10.0f} {r['time_cost']:>2} {r['parallelism']:>2} | "
            f"{r['hash']['p50']:>8.1f} {r['hash']['max']:>8.1f} | "
            f"{r['burst']['p50']:>9.1f} {r['burst']['max']:>9.1f} | "
            f"{r['peak_memory_mib']:>8.1f} | {r['stall_ms']:>6.1f}"
        )
    print("\ntimes in ms; peak MiB is above the process's use before any hashing")


if __name__ == "__main__":
    main()
