"""Tests against real model servers on this machine. Skipped unless asked for.

    scripts/run_local_models.sh start            # or use Ollama, see docs/12-real-models.md
    GATEWAY_REAL_MODELS=1 uv run pytest -m real_models -v

The servers come from the backends file (GATEWAY_BACKENDS, or the packaged one). These
tests check that the plumbing works with a real model. They assert nothing about speed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.app import create_default_app
from gateway.profiles import Tier

pytestmark = pytest.mark.real_models

PROMPT = "Reply with one short sentence: what is a radix tree?"


@pytest.fixture
def real_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_BACKEND", "llamacpp")


@pytest.mark.parametrize("tier", [t.value for t in Tier])
async def test_each_tier_generates_text_with_plausible_usage_and_timing(
    real_backend: None, tier: str
) -> None:
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=None
    ) as client:
        response = await client.post(
            "/v1/generate",
            json={"tenant_id": "acme", "model": tier, "prompt": PROMPT, "max_tokens": 24},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == tier
    assert body["text"].strip()
    assert "[simulated output" not in body["text"]
    assert 1 <= body["usage"]["completion_tokens"] <= 24
    assert body["usage"]["prompt_tokens"] >= 5
    assert 0 < body["timing"]["ttft_ms"] <= body["timing"]["total_ms"]


async def test_streaming_delivers_the_text_piece_by_piece(real_backend: None) -> None:
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=None
    ) as client:
        response = await client.post(
            "/v1/generate",
            json={
                "tenant_id": "acme",
                "model": "economy",
                "prompt": PROMPT,
                "max_tokens": 16,
                "stream": True,
            },
        )

    assert response.status_code == 200, response.text
    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1][6:]))
        for block in response.text.split("\n\n")
        if block
    ]
    tokens = [data for name, data in events if name == "token"]
    assert events[-1][0] == "done"
    assert tokens
    assert "".join(t["token"] for t in tokens) == events[-1][1]["text"]
    times = [t["t_ms"] for t in tokens]
    assert times == sorted(times)
