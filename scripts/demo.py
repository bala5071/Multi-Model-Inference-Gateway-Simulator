"""A two-second tour of the gateway, in simulator mode, with nothing to set up.

    uv run python scripts/demo.py        (or: make demo)

It builds the default simulated wiring in this process and calls it the way a client
would: three applications on three plans, a request outside a plan, a prompt-cache hit, a
streamed answer, a key that matches nothing and the usage ledger. No server is started and
no model runs; timings are simulated waits from the calibrated model profiles; prices are
placeholders.

The applications, their keys and the pepper exist only in this process's memory. The keys
are handed to the client below and never printed or written anywhere.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import httpx
from fastapi import FastAPI

from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import (
    DEFAULT_MAX_QUEUE_LEN,
    DEFAULT_OVERLOAD,
    DEFAULT_PREFILL_CHUNK_TOKENS,
    create_app,
)
from gateway.applications import InMemoryApplicationStore, seed_demo_applications
from gateway.clock import SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.routing import load_plans
from gateway.state import run_now

SYSTEM = "You are a careful assistant for a logistics company. " * 12
QUESTION = "Summarise the delivery policy in one sentence."


# Well formed, but no application has it.
UNKNOWN_KEY = "gwk_" + "0" * 16 + "_" + "A" * 43


def build() -> tuple[FastAPI, dict[str, str]]:
    """The default simulated wiring, with the demo applications and a throwaway key each."""
    clock = WallClock()
    registry = build_continuous_registry(
        clock,
        seed=0,
        max_queue_len=DEFAULT_MAX_QUEUE_LEN,
        prefill_chunk_tokens=DEFAULT_PREFILL_CHUNK_TOKENS,
    )
    templates = load_plans()
    store = InMemoryApplicationStore()
    epoch = SystemEpoch()
    run_now(seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms()))
    keys = ApiKeyService(store, secrets.token_bytes(32), epoch)
    issued = {
        a.id: run_now(keys.issue(a.id, "demo (in memory)")).key
        for a in run_now(store.list_applications())
    }
    access = ApplicationAccess(keys, templates)
    return create_app(registry, clock, applications=access, overload=DEFAULT_OVERLOAD), issued


def show(title: str, response: httpx.Response) -> None:
    print(f"\n== {title}")
    print(f"   HTTP {response.status_code}", end="")
    for header in ("X-Model-Used", "X-Cost-USD", "X-Downgraded-From", "Retry-After"):
        if header in response.headers:
            print(f"  {header}: {response.headers[header]}", end="")
    print()
    body: Any = response.json()
    if "error" in body:
        print(f"   {body['error']['code']}: {body['error']['message']}")
    elif "usage" in body:
        usage, timing = body["usage"], body["timing"]
        print(
            f"   model={body['model']}  prompt={usage['prompt_tokens']} "
            f"(cached {usage['cached_tokens']})  completion={usage['completion_tokens']}  "
            f"ttft={timing['ttft_ms']:.0f} ms  total={timing['total_ms']:.0f} ms  "
            f"cost=${body['cost']['usd']:.8f}"
        )
    else:
        print("   " + json.dumps(body)[:200])


async def main() -> None:
    app, keys = build()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:

        async def generate(application: str, **body: Any) -> httpx.Response:
            key = keys.get(application, UNKNOWN_KEY)
            return await client.post(
                "/v1/generate",
                json={"max_tokens": 24, **body},
                headers={"Authorization": f"Bearer {key}"},
            )

        print(
            "Simulator mode: no model runs. Timings are simulated on the calibrated profiles; "
            "prices are placeholders, not data."
        )
        for tenant, plan in (("initech", "free"), ("globex", "pro"), ("acme", "enterprise")):
            show(
                f'{tenant} ({plan} plan) asks for quality "best"',
                await generate(tenant, quality="best", prompt=QUESTION),
            )

        show(
            "initech (free plan) asks for the premium model by name",
            await generate("initech", model="premium", prompt=QUESTION),
        )

        long_prompt = SYSTEM + QUESTION
        show(
            "acme sends a long prompt",
            await generate("acme", model="standard", prompt=long_prompt),
        )
        show(
            "acme sends the same system prompt with another question (prefix cache hit)",
            await generate("acme", model="standard", prompt=SYSTEM + "And the returns policy?"),
        )

        print("\n== acme streams an answer (server-sent events)")
        stream = await generate("acme", model="economy", prompt=QUESTION, max_tokens=4, stream=True)
        for block in stream.text.strip().split("\n\n"):
            event, data = block.split("\n")
            print(f"   {event.removeprefix('event: '):5} {data.removeprefix('data: ')[:110]}")

        show(
            "a key that matches no application",
            await generate("nobody", quality="best", prompt=QUESTION),
        )

        usage = (
            await client.get(
                "/v1/applications/me/usage", headers={"Authorization": f"Bearer {keys['acme']}"}
            )
        ).json()
        print("\n== acme's usage this period")
        print(
            f"   plan={usage['plan']}  requests={usage['total']['requests']}  "
            f"billed tokens={usage['used_tokens']} of {usage['budget_tokens']}  "
            f"billed=${usage['total']['billed_cost_usd']:.8f}  "
            f"by model={ {m: u['requests'] for m, u in usage['by_model'].items()} }"
        )


if __name__ == "__main__":
    asyncio.run(main())
