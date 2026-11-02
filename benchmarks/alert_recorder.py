"""What one alert recorder call costs on the request path (docs/43).

MEASURED on the machine it runs on: real time, in process, no I/O (the recorder does none).
Each row times one ``AlertRecorder.record`` call, as the gateway makes it once per keyed
request, over 100 applications:

- "answered, flushed": a 200 with both latencies, into the recorder's own deltas (the Valkey
  case: the flusher takes them every 10 s);
- "answered, in memory": the same, written straight into the in-memory rollups;
- "chat, fell back": a chat request with a fallback, hosted attempts and a hosted answer;
- "refused": a 429 with its code.

    uv run python -m benchmarks.alert_recorder
    uv run python -m benchmarks.alert_recorder --calls 20000
"""

from __future__ import annotations

import argparse
import itertools
from collections.abc import Callable, Sequence

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.alertrecorder import AlertRecorder
from gateway.alertrollups import InMemoryAlertRollups
from gateway.clock import SystemEpoch, WallClock
from gateway.metrics import percentile

WARMUP_CALLS = 200
APPLICATIONS = 100


def _times(name: str, count: int, step: Callable[[], object]) -> WriteTimes:
    clock = WallClock()
    for _ in range(WARMUP_CALLS):
        step()
    times = []
    for _ in range(count):
        started = clock.now_ms()
        step()
        times.append(clock.now_ms() - started)
    return WriteTimes(
        store=name,
        writes=count,
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


def run(calls: int) -> list[WriteTimes]:
    epoch = SystemEpoch()
    applications = itertools.cycle([f"app{i}" for i in range(APPLICATIONS)])
    flushed = AlertRecorder(epoch)
    in_memory = AlertRecorder(epoch, sink=InMemoryAlertRollups())

    def answered(recorder: AlertRecorder) -> Callable[[], None]:
        def step() -> None:
            recorder.record(
                next(applications), kind="generate", status=200, ttft_ms=42.0, total_ms=380.0
            )

        return step

    def fell_back() -> None:
        flushed.record(
            next(applications),
            kind="chat",
            status=200,
            ttft_ms=120.0,
            total_ms=900.0,
            fallbacks=("connect_error",),
            hosted_attempts=2,
            hosted=True,
        )

    def refused() -> None:
        flushed.record(next(applications), kind="generate", status=429, code="rate_limited")

    return [
        _times("answered, flushed", calls, answered(flushed)),
        _times("answered, in memory", calls, answered(in_memory)),
        _times("chat, fell back", calls, fell_back),
        _times("refused", calls, refused),
    ]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time one alert recorder call.")
    parser.add_argument("--calls", type=int, default=10_000)
    args = parser.parse_args(argv)
    if args.calls < 1:
        parser.error("--calls must be at least 1")

    machine = describe_machine()
    print("MEASURED on this machine: real time, in process, no I/O.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"warm-up per row: {WARMUP_CALLS}; {APPLICATIONS} applications; nearest rank")
    print()
    print(f"{'what':<22} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in run(args.calls):
        print(
            f"{r.store:<22} {r.writes:>6} {r.mean_ms:>9.4f} {r.p50_ms:>9.4f} "
            f"{r.p99_ms:>9.4f} {r.max_ms:>9.4f}"
        )


if __name__ == "__main__":
    main()
