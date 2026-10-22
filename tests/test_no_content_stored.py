"""Nothing the gateway keeps at runtime contains a prompt, a response or token ids.

CLAUDE.md hard rule 7. Requests with distinctive prompt words go through the whole app,
in simulator mode (where the prefix cache holds the prompt's hashed token ids) and in
front of a mock real model server (which returns real text). Then every place the gateway
keeps or reports something is searched: the usage database's file bytes and rows, every
log record, every event, the monitoring snapshot, the Prometheus and summary metrics and
the usage endpoint.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.backend_config import build_llamacpp_registry
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.monitoring import Monitor
from gateway.registry import ModelRegistry
from gateway.tokenizer import WordTokenizer
from gateway.usage import SqliteUsageStore
from helpers import application_access, bearer
from mock_llama import MockLlamaServer
from test_real_backend_api import CONFIG

# Words that appear nowhere else in the gateway, its fixtures or SQLite's file format.
PROMPT = "Zorblax quintessa marmalith vexorian plumbrook ostrayle gwendolux"
RESPONSE_PIECES = ("Fizzwhack", " drumbleton", " quarnival")


def _needles(prompt: str, response_texts: list[str]) -> dict[str, list[bytes]]:
    """What must not appear, as the byte strings to look for."""
    ids = WordTokenizer().encode(prompt)
    return {
        "prompt text": [w.encode() for w in prompt.split()],
        "response text": [t.strip().encode() for t in response_texts if t.strip()],
        "token ids": [
            form
            for token in ids
            for form in (
                str(token).encode(),
                f"{token:x}".encode(),
                token.to_bytes(8, "big"),
                token.to_bytes(8, "little"),
            )
        ],
    }


def _database_bytes(path: Path, connection: sqlite3.Connection) -> bytes:
    rows = []
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        rows.append(repr(connection.execute(f"SELECT * FROM {table}").fetchall()))
    files = b"".join(p.read_bytes() for p in sorted(path.parent.glob(path.name + "*")))
    return files + "\n".join(rows).encode()


async def _exercise(
    registry: ModelRegistry, clock: SimClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    db_path = tmp_path / "gateway.db"
    connection = open_database(db_path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock)).migrate()
    store = SqliteUsageStore(connection)
    access, keys = application_access(clock)
    app = create_app(
        registry,
        clock,
        applications=access,
        usage_store=store,
        epoch=ClockEpoch(clock, origin_ms=1.7e12),
    )
    caplog.set_level(logging.DEBUG)
    answers = []
    reported = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        client.headers.update(bearer(keys["acme"]))
        body = {"model": "economy", "prompt": PROMPT, "max_tokens": 4}
        for _ in range(2):  # the second is served from the prefix cache in simulator mode
            response = await client.post("/v1/generate", json=body)
            assert response.status_code == 200
            answers.append(response.json())
        streamed = await client.post("/v1/generate", json={**body, "stream": True})
        assert streamed.status_code == 200
        for path in (
            "/v1/monitor/snapshot?history_s=900&events=1000",
            "/metrics",
            "/metrics/summary",
            "/v1/applications/me/usage",
        ):
            reported.append((await client.get(path)).content)
    await registry.aclose()

    # Three requests, three usage rows: the database was really written to.
    assert [s.event.request_id for s in store.events()] == ["req-1", "req-2", "req-3"]
    monitoring: Monitor = app.state.monitoring
    events = [dataclasses.asdict(e) for e in monitoring.events.since(0).events]
    places = {
        "usage database (open)": _database_bytes(db_path, connection),
        "log records": "\n".join(
            f"{r.name} {r.getMessage()} {r.args!r} {r.__dict__!r}" for r in caplog.records
        ).encode(),
        "events": json.dumps(events).encode(),
        "monitoring and usage endpoints": b"\n".join(reported),
    }
    connection.close()
    places["usage database (closed)"] = _database_bytes(db_path, open_database(db_path))
    return places, answers


def _assert_absent(places: dict[str, bytes], needles: dict[str, list[bytes]]) -> None:
    found = [
        f"{kind} found in {place}: {needle!r}"
        for place, haystack in places.items()
        for kind, forms in needles.items()
        for needle in forms
        if needle in haystack
    ]
    assert not found, "\n".join(found)


async def test_simulator_mode_stores_no_prompt_or_hashed_token_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=0)
    places, answers = await _exercise(registry, clock, tmp_path, caplog)

    # The prefix cache really held the prompt's hashed token ids...
    assert answers[1]["usage"]["cached_tokens"] == len(PROMPT.split())
    _assert_absent(places, _needles(PROMPT, [a["text"] for a in answers]))


async def test_real_backend_mode_stores_no_prompt_or_response_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, pieces=RESPONSE_PIECES)
    registry = build_llamacpp_registry(CONFIG, clock, client=server.client())
    places, answers = await _exercise(registry, clock, tmp_path, caplog)

    assert answers[0]["text"] == "".join(RESPONSE_PIECES)
    _assert_absent(places, _needles(PROMPT, list(RESPONSE_PIECES)))


def test_the_search_finds_what_it_looks_for() -> None:
    needles = _needles(PROMPT, list(RESPONSE_PIECES))
    token = WordTokenizer().encode(PROMPT)[0]
    for planted in (b"x Zorblax y", b"drumbleton", token.to_bytes(8, "big"), str(token).encode()):
        with pytest.raises(AssertionError):
            _assert_absent({"planted": planted}, needles)
