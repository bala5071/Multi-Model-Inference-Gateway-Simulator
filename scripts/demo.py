"""A two-second tour of the gateway, in simulator mode, with nothing to set up.

    uv run python scripts/demo.py        (or: make demo)

It builds the default app in this process and calls it the way a client would: three
tenants on three plans, a request outside a plan, a prompt-cache hit, a streamed answer
and the usage ledger. No server is started and no model runs; timings are simulated waits
from the calibrated model profiles; prices are placeholders.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from gateway.app import create_default_app

SYSTEM = "You are a careful assistant for a logistics company. " * 12
QUESTION = "Summarise the delivery policy in one sentence."


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
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:

        async def generate(**body: Any) -> httpx.Response:
            return await client.post("/v1/generate", json={"max_tokens": 24, **body})

        print(
            "Simulator mode: no model runs. Timings are simulated on the calibrated profiles; "
            "prices are placeholders, not data."
        )
        for tenant, plan in (("initech", "free"), ("globex", "pro"), ("acme", "enterprise")):
            show(
                f'{tenant} ({plan} plan) asks for quality "best"',
                await generate(tenant_id=tenant, quality="best", prompt=QUESTION),
            )

        show(
            "initech (free plan) asks for the premium model by name",
            await generate(tenant_id="initech", model="premium", prompt=QUESTION),
        )

        long_prompt = SYSTEM + QUESTION
        show(
            "acme sends a long prompt",
            await generate(tenant_id="acme", model="standard", prompt=long_prompt),
        )
        show(
            "acme sends the same system prompt with another question (prefix cache hit)",
            await generate(
                tenant_id="acme", model="standard", prompt=SYSTEM + "And the returns policy?"
            ),
        )

        print("\n== acme streams an answer (server-sent events)")
        stream = await generate(
            tenant_id="acme", model="economy", prompt=QUESTION, max_tokens=4, stream=True
        )
        for block in stream.text.strip().split("\n\n"):
            event, data = block.split("\n")
            print(f"   {event.removeprefix('event: '):5} {data.removeprefix('data: ')[:110]}")

        show("unknown tenant", await generate(tenant_id="nobody", quality="best", prompt=QUESTION))

        usage = (await client.get("/v1/tenants/acme/usage")).json()
        print("\n== acme's usage this period")
        print(
            f"   plan={usage['plan']}  requests={usage['total']['requests']}  "
            f"billed tokens={usage['used_tokens']} of {usage['budget_tokens']}  "
            f"billed=${usage['total']['billed_cost_usd']:.8f}  "
            f"by model={ {m: u['requests'] for m, u in usage['by_model'].items()} }"
        )


if __name__ == "__main__":
    asyncio.run(main())
