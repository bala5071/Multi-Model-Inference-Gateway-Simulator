"""What the chat endpoint adds in front of a provider, as docs/31 quotes.

MEASURED on the machine it runs on: real time, real CPU, through HTTP. Each run starts two
processes on this machine: a mock OpenAI-compatible provider (this module with ``--serve-mock``:
it answers every chat request at once with the same short streamed answer, so what is timed is
not a model) and the real gateway (``python -m gateway.serve``, ``GATEWAY_BACKEND=llamacpp``,
every tier's chat adapter pointed at the mock), on a throwaway database with a throwaway
pepper, admin and application key. Then, interleaved round by round, from a client on the same
machine:

- "mock directly": the streaming request the adapter sends, read to the end;
- "through the gateway": ``POST /v1/chat/completions`` (not streamed) for the same tier;
- "first text, mock" and "first text, gateway": the same two, streamed, timed to the first
  chunk that carries text.

The overheads are the gateway's figure minus the mock's, p50 against p50: the gateway's own
call to the mock is part of the gateway's figure. They say nothing about any real provider's
latency: the provider is a mock.

    uv run python -m benchmarks.chat_overhead
    uv run python -m benchmarks.chat_overhead --rounds 500 --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx

from benchmarks.calibrate import describe_machine
from benchmarks.login import _free_port
from gateway.apikeys import ApiKeyService
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.routing import load_plans

WARMUP_ROUNDS = 20
KINDS = (
    "mock directly",
    "through the gateway",
    "first text, mock",
    "first text, gateway",
)
DIFFERENCES = {
    "overhead, total": ("through the gateway", "mock directly"),
    "overhead, first text": ("first text, gateway", "first text, mock"),
}
REQUEST = {"model": "economy", "messages": [{"role": "user", "content": "Say hello."}]}


def mock_app() -> object:
    """A provider that streams "Hello there!" in three pieces, with usage, and no delay."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    async def chat(request: Request) -> StreamingResponse:
        await request.body()

        async def events() -> AsyncIterator[str]:
            for piece in ("Hello", " there", "!"):
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            usage = {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}
            yield f"data: {json.dumps(usage)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])])


def _wait_for(url: str) -> None:
    pause = threading.Event()
    for _ in range(300):
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.TransportError:
            pause.wait(0.1)
    raise RuntimeError(f"{url} did not start")


def _prepare(path: Path, pepper: str) -> str:
    """A migrated database with the demo applications; returns a key for one of them."""
    connection = open_database(path)
    epoch = SystemEpoch()
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    seed_demo_applications(store, load_plans(), created_at_ms=epoch.epoch_ms())
    key = ApiKeyService(store, pepper.encode(), epoch).issue("acme", "bench").key
    connection.close()
    return key


def _text_of(data: str) -> str:
    """The text a chunk carries, if any."""
    choices = json.loads(data).get("choices") or [{}]
    delta = choices[0].get("delta") or {}
    text = delta.get("content")
    return text if isinstance(text, str) else ""


async def _first_content_ms(
    client: httpx.AsyncClient, clock: WallClock, url: str, body: dict[str, object], **kw: object
) -> tuple[float, float]:
    """(time to the first chunk carrying text, time to the end) of a streamed request."""
    started = clock.now_ms()
    first: float | None = None
    async with client.stream("POST", url, json=body, **kw) as response:  # type: ignore[arg-type]
        if response.status_code != 200:
            raise RuntimeError(f"{url} answered {response.status_code}")
        async for line in response.aiter_lines():
            if first is None and line.startswith("data: {") and _text_of(line[6:]):
                first = clock.now_ms() - started
    total = clock.now_ms() - started
    if first is None:
        raise RuntimeError(f"{url} sent no text")
    return first, total


async def _measure(mock: str, gateway: str, key: str, rounds: int) -> dict[str, list[float]]:
    clock = WallClock()
    results: dict[str, list[float]] = {kind: [] for kind in KINDS}
    upstream = {
        "model": "default",
        "messages": REQUEST["messages"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for round_ in range(WARMUP_ROUNDS + rounds):
            first_direct, direct = await _first_content_ms(
                client, clock, f"{mock}/v1/chat/completions", upstream
            )
            started = clock.now_ms()
            answer = await client.post(f"{gateway}/v1/chat/completions", json=REQUEST, headers=auth)
            through = clock.now_ms() - started
            if answer.status_code != 200:
                raise RuntimeError(f"the gateway answered {answer.status_code}")
            first_through, _ = await _first_content_ms(
                client,
                clock,
                f"{gateway}/v1/chat/completions",
                {**REQUEST, "stream": True},
                headers=auth,
            )
            if round_ >= WARMUP_ROUNDS:
                for kind, value in zip(
                    KINDS, (direct, through, first_direct, first_through), strict=True
                ):
                    results[kind].append(value)
    return results


def _run(rounds: int, environ: dict[str, str]) -> dict[str, list[float]]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        mock_port, gateway_port = _free_port(), _free_port()
        mock = subprocess.Popen(
            [sys.executable, "-m", "benchmarks.chat_overhead", "--serve-mock", str(mock_port)],
            env=environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        servers = "\n".join(
            f"    {tier}:\n      url: http://127.0.0.1:{mock_port}"
            for tier in ("economy", "standard", "premium")
        )
        backends = Path(folder) / "backends.yaml"
        backends.write_text(
            f"backend: llamacpp\nllamacpp:\n  chat_api: openai_compatible\n  servers:\n{servers}\n",
            encoding="utf-8",
        )
        pepper = secrets.token_urlsafe(32)
        key = _prepare(Path(folder) / "gateway.db", pepper)
        server_env = {
            **environ,
            "GATEWAY_DATABASE": str(Path(folder) / "gateway.db"),
            "GATEWAY_KEY_PEPPER": pepper,
            "GATEWAY_BACKENDS": str(backends),
            "GATEWAY_ADMIN_USERNAME": "benchadmin",
            "GATEWAY_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        }
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gateway.serve",
                "--port",
                str(gateway_port),
                "--log-level",
                "warning",
            ],
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for(f"http://127.0.0.1:{mock_port}/")
            _wait_for(f"http://127.0.0.1:{gateway_port}/healthz")
            return asyncio.run(
                _measure(
                    f"http://127.0.0.1:{mock_port}",
                    f"http://127.0.0.1:{gateway_port}",
                    key,
                    rounds,
                )
            )
        finally:
            for process in (gateway, mock):
                process.terminate()
                process.wait(timeout=30)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time the chat endpoint against a mock provider.")
    parser.add_argument("--rounds", type=int, default=500)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--serve-mock", type=int, metavar="PORT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve_mock is not None:
        import uvicorn

        uvicorn.run(mock_app(), host="127.0.0.1", port=args.serve_mock, log_level="warning")  # type: ignore[arg-type]
        return
    if args.rounds < 1 or args.runs < 1:
        parser.error("--rounds and --runs must be at least 1")
    machine = describe_machine()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    print("MEASURED on this machine: real time, real CPU, over HTTP; the provider is a mock.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(
        f"{args.runs} runs of {args.rounds} rounds each, after {WARMUP_ROUNDS} warm-up rounds;"
        " times in ms; percentiles: nearest rank"
    )
    p50s: dict[str, list[float]] = {kind: [] for kind in (*KINDS, *DIFFERENCES)}
    for run in range(1, args.runs + 1):
        results = _run(args.rounds, environ)
        print(f"run {run}")
        for kind in KINDS:
            times = results[kind]
            p50s[kind].append(percentile(times, 50))
            print(
                f"  {kind:<22} p50 {percentile(times, 50):>7.2f}  p90 {percentile(times, 90):>7.2f}"
                f"  mean {sum(times) / len(times):>7.2f}  max {max(times):>7.2f}"
            )
        for name, (gateway_kind, mock_kind) in DIFFERENCES.items():
            p50s[name].append(p50s[gateway_kind][-1] - p50s[mock_kind][-1])
            print(f"  {name:<22} p50 {p50s[name][-1]:>7.2f}")
    print("p50 across runs (min to max)")
    for kind, values in p50s.items():
        print(f"  {kind:<22} {min(values):>7.2f} to {max(values):>7.2f}")


if __name__ == "__main__":
    main()
