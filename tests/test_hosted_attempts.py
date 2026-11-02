"""Scale-out PR 2b on the chat path (docs/41): the operator's cap on one request's hosted
attempts, a half-open probe whose mode re-read is cancelled or fails freed at once, the
shutdown flush of the breakers' reports, the dropped-report metric, and the leak scan. The
queue on its own is in ``test_breaker_reports.py``; the counter calls on the path in
``test_chat_routing.py``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from gateway.breaker import Admission
from gateway.breakerreports import DropReason, Report
from gateway.chatrouter import (
    MAX_HOSTED_ATTEMPTS_ENV,
    ChatRouter,
    FallbackReason,
    RouteTrace,
    max_hosted_attempts_from,
)
from helpers import default_server
from mock_network import sse_response as sse
from mock_providers import openai_text
from test_chat_routing import (
    BAD_TOOL,
    FIVE_XX,
    KEY,
    PRIMARY,
    TEXT,
    A,
    B,
    P,
    Routing,
    routing,
)

D = "prv_00000000000000d4"
HOSTS = ((A, "a"), (B, "b"), (P, "p"), (D, "d"))


def _traces(monkeypatch: pytest.MonkeyPatch) -> list[RouteTrace]:
    """Every request's route trace, as the router filled it in."""
    seen: list[RouteTrace] = []
    real = ChatRouter.run

    async def run(self: ChatRouter, *args: Any, **kwargs: Any) -> Any:
        trace = kwargs["trace"] if "trace" in kwargs else args[5]
        assert isinstance(trace, RouteTrace)
        seen.append(trace)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(ChatRouter, "run", run)
    return seen


async def _providers(r: Routing, count: int, **kwargs: Any) -> None:
    # D's name, which the routing tests' resolver does not have: public, as theirs are.
    r.resolver.table.setdefault("d.provider.test", ["93.184.216.37"])
    for provider_id, host in HOSTS[:count]:
        await r.provider(provider_id, f"{host}.provider.test", role="primary", **kwargs)


def _contacted(r: Routing) -> list[str]:
    """The provider hosts actually connected to, in order (each attempt is a connection)."""
    return [c.tls_host or c.address for c in r.org_net.connections]


# --- the setting ----------------------------------------------------------------------------


def test_the_cap_is_3_by_default_and_1_to_5_when_set() -> None:
    assert max_hosted_attempts_from({}) == 3
    for value in ("1", "5", " 2 "):
        assert max_hosted_attempts_from({MAX_HOSTED_ATTEMPTS_ENV: value}) == int(value)
    # The last is ARABIC-INDIC DIGIT THREE: a digit to str.isdigit, not a whole number here.
    for value in ("0", "6", "3.0", "three", "-1", "", chr(0x0663)):
        with pytest.raises(ValueError, match=MAX_HOSTED_ATTEMPTS_ENV):
            max_hosted_attempts_from({MAX_HOSTED_ATTEMPTS_ENV: value})


def test_a_cap_out_of_range_refuses_the_server_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_HOSTED_ATTEMPTS_ENV, "9")
    with pytest.raises(ValueError, match=MAX_HOSTED_ATTEMPTS_ENV), default_server():
        pass


async def test_the_router_refuses_a_cap_out_of_range() -> None:
    for value in (0, 6):
        with pytest.raises(ValueError, match=MAX_HOSTED_ATTEMPTS_ENV):
            async with routing(max_hosted_attempts=value):
                pass


# --- at the cap -----------------------------------------------------------------------------


async def test_four_failing_primaries_three_are_tried_and_the_fourth_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four primary providers that all fail: three are tried, the fourth is skipped as
    ``attempt_cap`` without a call or a question to its breaker, and the gateway's own tier
    answers, which the mode allows."""
    traces = _traces(monkeypatch)
    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 4)
        r.org_net.responses = [FIVE_XX] * 4
        answer = await r.chat()
        contacted = _contacted(r)
        admitted_d = await r.router.breakers.state("north", D)
    assert answer.status_code == 200, answer.text
    assert answer.headers["x-provider"] == "gateway"
    assert contacted == ["a.provider.test", "b.provider.test", "p.provider.test"]
    assert traces[-1].reasons == [
        FallbackReason.UPSTREAM_5XX,
        FallbackReason.UPSTREAM_5XX,
        FallbackReason.UPSTREAM_5XX,
        FallbackReason.ATTEMPT_CAP,
    ]
    assert answer.headers["x-fallback-reason"] == "upstream_5xx"  # the first, as before
    assert admitted_d.value == "closed"


async def test_the_retry_after_invalid_tool_json_counts_as_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traces = _traces(monkeypatch)
    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 3, tools=True)
        r.org_net.responses = [BAD_TOOL, BAD_TOOL, FIVE_XX]
        answer = await r.chat(tools=True)
        contacted = _contacted(r)
    # The simulated gateway tier takes no tools, so nothing local is left after the cap.
    assert answer.status_code == 502, answer.text
    assert contacted == ["a.provider.test", "a.provider.test", "b.provider.test"]
    assert traces[-1].reasons == [
        FallbackReason.INVALID_TOOL_JSON,
        FallbackReason.UPSTREAM_5XX,
        FallbackReason.ATTEMPT_CAP,
    ]


async def test_at_a_cap_of_one_the_retry_is_not_made(monkeypatch: pytest.MonkeyPatch) -> None:
    traces = _traces(monkeypatch)
    async with routing(operator=None, max_hosted_attempts=1) as r:
        await r.mode(PRIMARY)
        await _providers(r, 2, tools=True)
        r.org_net.responses = [BAD_TOOL, TEXT]
        answer = await r.chat(tools=True)
        contacted = _contacted(r)
    assert answer.status_code == 502, answer.text
    assert contacted == ["a.provider.test"]
    assert traces[-1].reasons == [FallbackReason.INVALID_TOOL_JSON, FallbackReason.ATTEMPT_CAP]


async def test_a_provider_skipped_without_a_call_is_not_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open breaker skips A without a call: B, P and D are the three attempts."""
    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 4)
        for _ in range(5):
            await r.router.breakers.failure("north", A)
        r.org_net.responses = [FIVE_XX, FIVE_XX, sse(openai_text("Hi").chunks)]
        answer = await r.chat()
        contacted = _contacted(r)
    assert answer.headers["x-provider"] == D
    assert contacted == ["b.provider.test", "p.provider.test", "d.provider.test"]


# --- a probe whose mode re-read is cancelled or fails ------------------------------------------


async def _half_open(r: Routing, *provider_ids: str) -> None:
    for provider_id in provider_ids:
        for _ in range(5):
            await r.router.breakers.failure("north", provider_id)
    await r.clock.sleep_ms(30_000.0)


def _hold_re_read(r: Routing, at: int, then: BaseException | None) -> asyncio.Event:
    """The ``at``-th re-read just before a hosted attempt (``hosted_policy``, counting from 1)
    waits forever, or raises ``then``; the returned event is set when it is reached."""
    reads = 0
    reached = asyncio.Event()
    real = r.router._modes.hosted_policy

    async def hosted_policy(organization_id: str, application_id: str) -> Any:
        nonlocal reads
        reads += 1
        if reads == at:
            reached.set()
            if then is not None:
                raise then
            await asyncio.Event().wait()
        return await real(organization_id, application_id)

    r.router._modes.hosted_policy = hosted_policy  # type: ignore[method-assign]
    return reached


async def test_a_probe_cancelled_during_the_modes_re_read_frees_its_slot_at_once() -> None:
    """Found while planning PR 2b: A's breaker is half-open and this request is its probe; the
    client hangs up while the modes are read again just before the attempt (docs/38). Its
    admission is given back then: the next request may probe at once, not after 120 s (and in
    memory, before PR 2b, not until the process restarted)."""
    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 1)
        await _half_open(r, A)
        reached = _hold_re_read(r, at=1, then=None)
        request = asyncio.ensure_future(r.chat())
        await reached.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert r.org_net.connections == []  # it never reached A
        assert await r.router.breakers.admit_attempt("north", A) is Admission.PROBE


async def test_a_probe_whose_modes_re_read_fails_frees_its_slot_and_still_fails_closed() -> (
    None
):  # fmt: skip
    """The re-read cannot be answered (the store is down): the request is refused as before,
    503 ``state_unavailable``, A is never contacted, and A's slot is free at once."""
    from gateway.state import StateUnavailableError

    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 1)
        await _half_open(r, A)
        _hold_re_read(r, at=1, then=StateUnavailableError("down"))
        answer = await r.chat()
        assert (answer.status_code, answer.json()["error"]["code"]) == (503, "state_unavailable")
        assert r.org_net.connections == []
        assert await r.router.breakers.admit_attempt("north", A) is Admission.PROBE


async def test_another_requests_probe_is_never_released() -> None:
    """A is half-open and the first request is its probe, still waiting for A's answer. The
    second request is refused A (circuit_open), is admitted to B, and its re-read before B
    fails: what it gives back is its own admission to B, never the first request's slot on A,
    which stays taken until the first request reports."""
    from gateway.state import StateUnavailableError
    from mock_network import Gate

    async with routing(operator=None) as r:
        await r.mode(PRIMARY)
        await _providers(r, 2)
        await _half_open(r, A)
        gate = Gate(TEXT)
        r.org_net.responses = [gate]
        first = asyncio.ensure_future(r.chat())
        await gate.reached.wait()  # the first request is A's probe, at A
        # Counted from here: the second request's re-read before B is the first.
        _hold_re_read(r, at=1, then=StateUnavailableError("down"))
        second = await r.chat()
        assert second.status_code == 503
        breakers = r.router.breakers
        assert await breakers.admit_attempt("north", A) is Admission.REFUSED  # still taken
        gate.opened.set()
        answered = await first
        assert answered.headers["x-provider"] == A
        assert await breakers.admit_attempt("north", A) is Admission.PROBE  # reported: free


# --- the shutdown flush and the metric -----------------------------------------------------------


async def test_the_lifespan_flushes_the_queued_reports_before_it_ends() -> None:
    """A report still being written when the server stops is written by the shutdown flush,
    on the injected clock, before the counters' client would close; after it, nothing more is
    queued."""
    async with routing(operator=None) as r:
        breakers = r.router.breakers
        written: list[str] = []
        real = breakers.failure

        async def slow_failure(organization_id: str, provider_id: str) -> bool:
            await r.clock.sleep_ms(500.0)
            written.append(provider_id)
            return await real(organization_id, provider_id)

        breakers.failure = slow_failure  # type: ignore[method-assign]
        reports = r.router.reports
        assert reports is not None
        async with r.app.router.lifespan_context(r.app):
            reports.enqueue(*_failure(A))
            reports.enqueue(*_failure(B))
            assert written == []
        assert written == [A, B]
        reports.enqueue(*_failure(P))
        assert reports.queued == 0
        assert reports.dropped[DropReason.QUEUE_FULL] == 1


def _failure(provider_id: str) -> tuple[Report, str, str]:
    return Report.FAILURE, "north", provider_id


async def test_a_dropped_report_is_in_the_snapshot_and_in_prometheus() -> None:
    async with routing(operator=None) as r:
        reports = r.router.reports
        assert reports is not None
        before = (await r.client.get("/v1/monitor/snapshot")).json()["breakers"]
        assert before == {
            "available": True,
            "reports_dropped": {"queue_full": 0, "flush_timeout": 0, "unavailable": 0},
        }
        await reports.flush(0.0)
        reports.enqueue(*_failure(A))
        reports.enqueue(*_failure(B))
        after = (await r.client.get("/v1/monitor/snapshot")).json()["breakers"]
        metrics = (await r.client.get("/metrics")).text
    assert after["reports_dropped"] == {"queue_full": 2, "flush_timeout": 0, "unavailable": 0}
    assert 'gateway_breaker_reports_dropped_total{reason="queue_full"} 2' in metrics


# --- the leak scan --------------------------------------------------------------------------


async def test_no_provider_key_or_prompt_reaches_the_reports_logs_snapshot_or_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hosted requests through the cap and the queue, failing and succeeding, with a planted
    provider key (the vault's) and a planted prompt. The reports queued and sent, the log at
    DEBUG, the snapshot with its events, and the Prometheus text are scanned. The controls: the
    key and the prompt are found in what was sent to the providers."""
    planted_prompt = "plantedattemptprompt7e3a"
    queued: list[tuple[Any, ...]] = []
    with caplog.at_level(logging.DEBUG):
        async with routing(operator=None) as r:
            reports = r.router.reports
            assert reports is not None
            real_enqueue = reports.enqueue

            def enqueue(report: Report, organization_id: str, provider_id: str) -> None:
                queued.append((report, organization_id, provider_id))
                real_enqueue(report, organization_id, provider_id)

            reports.enqueue = enqueue  # type: ignore[method-assign]
            await r.mode(PRIMARY)
            await _providers(r, 4)
            r.org_net.responses = [*[FIVE_XX] * 3, sse(openai_text("Hi").chunks)]
            for _ in range(2):
                await r.client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "economy",
                        "messages": [{"role": "user", "content": planted_prompt}],
                    },
                    headers={"Authorization": f"Bearer {r.world.api_keys['north-shop']}"},
                )
            await reports.drained()
            snapshot = (await r.client.get("/v1/monitor/snapshot?events=1000")).text
            metrics = (await r.client.get("/metrics")).text
            sent = b"".join(bytes(c.written) for c in r.org_net.connections)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY.encode() in sent  # the controls
    assert planted_prompt.encode() in sent
    assert queued  # reports went through the queue
    for planted in (KEY, planted_prompt):
        assert planted not in repr(queued)
        assert planted not in logged
        assert planted not in snapshot
        assert planted not in metrics
