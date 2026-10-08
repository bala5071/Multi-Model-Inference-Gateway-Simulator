"""The calibration tool, against a fake model server with known timing parameters."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from benchmarks import calibrate as calibrate_module
from benchmarks.calibrate import (
    CalibrationPlan,
    CalibrationRun,
    Sample,
    _combine,
    aggregate,
    calibrate,
    describe_machine,
    describe_placement,
    describe_server,
    errors,
    errors_by_concurrency,
    fit_profile,
    hardware_limited_note,
    linear_fit,
    load_run,
    mape,
    median,
    simulate,
    spread_across_repeats,
    spread_across_runs,
    spread_summary,
    summary,
    write_outputs,
)

from gateway.app import create_default_app
from gateway.backend_config import (
    BackendConfig,
    BackendConfigError,
    LlamaServer,
    parse_backend_config,
)
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.profiles import PROFILES, STANDARD, Tier, load_calibrated_profiles
from gateway.schema import GenerateRequest
from mock_llama import GroundTruthServer

CONFIG = BackendConfig(
    kind="llamacpp", servers={tier: LlamaServer("http://127.0.0.1:9000") for tier in Tier}
)
PLAN = CalibrationPlan(
    prompt_words=(20, 100, 400),
    output_tokens=(8, 32),
    concurrency=(1, 2, 4),
    repeats=2,
    base_prompt_words=20,
    base_output_tokens=16,
)
# What the fake server does. The tool has to find these.
TRUTH = {"prefill_ms_per_token": 0.25, "decode_base_ms": 20.0, "decode_ms_per_extra_seq": 3.0}


def _server(clock: SimClock, *, overhead_ms: float = 0.0, **truth: float) -> GroundTruthServer:
    values = {**TRUTH, **truth}
    return GroundTruthServer(
        clock,
        prefill_ms_per_token=values["prefill_ms_per_token"],
        decode_base_ms=values["decode_base_ms"],
        decode_ms_per_extra_seq=values["decode_ms_per_extra_seq"],
        overhead_ms=overhead_ms,
    )


async def _calibrate(
    plan: CalibrationPlan = PLAN, *, overhead_ms: float = 0.0, **truth: float
) -> tuple[CalibrationRun, GroundTruthServer]:
    clock = SimClock()
    server = _server(clock, overhead_ms=overhead_ms, **truth)
    async with server.client() as client:
        run = await calibrate(
            CONFIG,
            plan,
            tiers=[Tier.STANDARD],
            clock=clock,
            client=client,
            run_id="test",
            measured_at="2026-01-01T00:00:00+00:00",
            machine={"cpu": "test"},
            notes="unit test",
        )
    return run, server


def _sample(**overrides: Any) -> Sample:
    values: dict[str, Any] = {
        "tier": "standard",
        "sweep": "prompt",
        "prompt_words": 10,
        "max_tokens": 8,
        "concurrency": 1,
        "repeat": 0,
        "holdout": False,
        "prompt_tokens": 10,
        "completion_tokens": 8,
        "ttft_ms": 100.0,
        "total_ms": 240.0,
        "per_token_ms": 20.0,
    }
    return Sample(**{**values, **overrides})


# --- arithmetic ----------------------------------------------------------------------------


def test_linear_fit_recovers_an_exact_line() -> None:
    fit = linear_fit([0, 1, 2, 3], [5.0, 7.0, 9.0, 11.0])
    assert (fit.slope, fit.intercept, fit.r2, fit.n) == (2.0, 5.0, 1.0, 4)


def test_linear_fit_reports_a_partial_r2_for_noisy_data() -> None:
    fit = linear_fit([0, 1, 2, 3], [0.0, 2.0, 1.0, 3.0])
    assert fit.slope == pytest.approx(0.8)
    assert fit.intercept == pytest.approx(0.3)
    assert fit.r2 == pytest.approx(0.64)


def test_linear_fit_cannot_estimate_a_slope_from_one_x_value() -> None:
    fit = linear_fit([3, 3, 3], [10.0, 20.0, 30.0])
    assert (fit.slope, fit.intercept, fit.r2) == (0.0, 20.0, None)


def test_linear_fit_rejects_empty_or_mismatched_input() -> None:
    with pytest.raises(ValueError, match="equally many"):
        linear_fit([], [])
    with pytest.raises(ValueError, match="equally many"):
        linear_fit([1, 2], [1.0])


def test_mape_is_the_mean_absolute_percentage_error() -> None:
    assert mape([100.0, 200.0], [110.0, 180.0]) == pytest.approx(10.0)
    assert mape([100.0], [100.0]) == 0.0
    assert mape([], []) is None
    assert mape([0.0], [5.0]) is None  # a zero measurement has no percentage error


# --- the plan ------------------------------------------------------------------------------


def test_plan_sweeps_one_thing_at_a_time_without_duplicates() -> None:
    scenarios = PLAN.scenarios()
    assert [(s.sweep, s.prompt_words, s.max_tokens, s.concurrency) for s in scenarios] == [
        ("prompt", 20, 8, 1),
        ("prompt", 100, 8, 1),
        ("prompt", 400, 8, 1),
        ("output", 20, 8, 1),
        ("output", 20, 32, 1),
        ("concurrency", 20, 16, 1),
        ("concurrency", 20, 16, 2),
        ("concurrency", 20, 16, 4),
    ]


@pytest.mark.parametrize(
    "overrides",
    [{"repeats": 0}, {"concurrency": ()}, {"concurrency": (0,)}, {"output_tokens": (1,)}],
)
def test_invalid_plan_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"need|at least"):
        dataclasses.replace(PLAN, **overrides)


# --- measuring and fitting -----------------------------------------------------------------


async def test_calibration_recovers_the_servers_true_parameters() -> None:
    # The server's first token comes 120 ms after the request, plus prefill. One 20 ms
    # decode step of that is already in the simulator, so the overhead is the other 100.
    run, server = await _calibrate(overhead_ms=120.0)
    profile = run.document["profiles"]["standard"]

    assert profile["prefill_ms_per_token"] == pytest.approx(0.25)
    assert profile["decode_base_ms"] == pytest.approx(20.0)
    assert profile["decode_ms_per_extra_seq"] == pytest.approx(3.0)
    assert profile["per_request_overhead_ms"] == pytest.approx(100.0)
    assert profile["fit"]["ttft_vs_prompt_tokens"]["r2"] == pytest.approx(1.0)
    assert profile["fit"]["per_token_vs_extra_sequences"]["r2"] == pytest.approx(1.0)
    assert profile["max_concurrency_measured"] == 4
    assert profile["failed_requests"] == 0
    assert profile["notes"] == []
    # 8 scenarios, 2 repeats, and 1 + 2 + 4 requests in the concurrency ones: 24, plus
    # the one warm-up request that is thrown away.
    assert profile["samples"] == 24
    assert server.requests == 25


async def test_samples_record_what_was_asked_and_what_was_measured() -> None:
    run, _ = await _calibrate()
    samples = run.tiers["standard"].samples

    by_key = {(s.sweep, s.prompt_words, s.max_tokens, s.concurrency, s.repeat): s for s in samples}
    longest = by_key[("prompt", 400, 8, 1, 0)]
    # The prompt is the 400 words plus a tag and a fixed instruction; the server counts
    # every word as a token, and that count is what the fit uses.
    assert longest.prompt_tokens > 400
    assert longest.completion_tokens == 8
    assert longest.ttft_ms == pytest.approx(0.25 * longest.prompt_tokens)
    assert longest.per_token_ms == pytest.approx(20.0)
    assert longest.total_ms == pytest.approx(longest.ttft_ms + 7 * 20.0)

    four = [s for s in samples if s.concurrency == 4 and s.repeat == 0]
    assert len(four) == 4
    assert all(s.per_token_ms == pytest.approx(20.0 + 3 * 3.0) for s in four)
    # The last repeat of every scenario is held out of the fit.
    assert {s.repeat for s in samples if s.holdout} == {1}
    assert {s.repeat for s in samples if not s.holdout} == {0}


async def test_no_two_requests_share_a_prompt_prefix() -> None:
    clock = SimClock()
    prompts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["prompt"])
        text = (
            'data: {"choices": [{"index": 0, "text": "a"}]}\n\n'
            'data: {"choices": [{"index": 0, "text": "b"}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=text.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await calibrate(
            CONFIG,
            PLAN,
            tiers=[Tier.STANDARD],
            clock=clock,
            client=client,
            run_id="run42",
            measured_at="t",
            machine={},
        )

    # A server-side prompt cache must not be able to skip any prompt processing.
    first_words = [p.split()[0] for p in prompts]
    assert len(set(first_words)) == len(prompts)
    assert all(word.startswith("[run42-standard-") for word in first_words)


async def test_single_concurrency_level_says_nothing_about_batching() -> None:
    run, _ = await _calibrate(dataclasses.replace(PLAN, concurrency=(1,)))
    profile = run.document["profiles"]["standard"]

    assert profile["decode_base_ms"] == pytest.approx(20.0)
    assert profile["decode_ms_per_extra_seq"] == 0.0
    assert profile["fit"]["per_token_vs_extra_sequences"]["r2"] is None
    assert any("only one concurrency level" in note for note in profile["notes"])


def test_fits_that_come_out_non_positive_are_clamped_and_flagged() -> None:
    samples = [
        # TTFT falls as prompts grow, and per-token time falls with concurrency: noise.
        _sample(prompt_tokens=10, ttft_ms=100.0, per_token_ms=20.0),
        _sample(prompt_tokens=1000, ttft_ms=90.0, per_token_ms=20.0),
        _sample(sweep="concurrency", concurrency=4, per_token_ms=15.0),
    ]
    fitted = fit_profile(samples, STANDARD)

    assert fitted.profile.prefill_ms_per_token == 1e-6
    assert fitted.profile.decode_ms_per_extra_seq == 0.0
    assert len(fitted.notes) == 2
    assert "not positive" in fitted.notes[0]
    assert "negative; set to 0" in fitted.notes[1]


def test_fit_warns_when_concurrent_requests_were_served_one_after_another() -> None:
    def sweep(concurrency: int, ttft_ms: float) -> Sample:
        return _sample(sweep="concurrency", concurrency=concurrency, ttft_ms=ttft_ms)

    # What a server with a single slot does: same per-token speed, but each request sent
    # with others waits for the ones ahead of it before its first token.
    prompts = [_sample(prompt_tokens=10, ttft_ms=40.0), _sample(prompt_tokens=110, ttft_ms=90.0)]
    queued = [*prompts, sweep(1, 40.0), *(sweep(4, t) for t in (45.0, 450.0, 850.0, 1250.0))]
    notes = fit_profile(queued, STANDARD).notes
    assert len(notes) == 1
    assert "with 4 requests sent together, the slowest waited 1250 ms" in notes[0]
    assert "a whole request takes alone (240 ms)" in notes[0]
    assert "at least 4 parallel slots" in notes[0]

    # A server that really batches them: first tokens come later than alone, even several
    # times later, but long before a whole request would have finished.
    batched = [*prompts, sweep(1, 40.0), *(sweep(4, t) for t in (48.0, 90.0, 120.0, 140.0))]
    assert fit_profile(batched, STANDARD).notes == ()


def test_fit_keeps_everything_calibration_does_not_measure() -> None:
    samples = [
        _sample(prompt_tokens=10, ttft_ms=30.0),
        _sample(prompt_tokens=110, ttft_ms=80.0),
        _sample(sweep="concurrency", concurrency=3, per_token_ms=26.0),
    ]
    profile = fit_profile(samples, STANDARD).profile

    assert (profile.prefill_ms_per_token, profile.decode_base_ms) == (0.5, 20.0)
    assert profile.decode_ms_per_extra_seq == pytest.approx(3.0)
    for unchanged in (
        "name",
        "tier",
        "price_per_1k_input_tokens",
        "price_per_1k_output_tokens",
        "max_batch_size",
        "kv_capacity_tokens",
    ):
        assert getattr(profile, unchanged) == getattr(STANDARD, unchanged)


def test_fit_ignores_held_out_samples() -> None:
    samples = [
        _sample(prompt_tokens=10, ttft_ms=30.0),
        _sample(prompt_tokens=110, ttft_ms=80.0),
        _sample(prompt_tokens=110, ttft_ms=9999.0, per_token_ms=999.0, holdout=True),
    ]
    assert fit_profile(samples, STANDARD).profile.prefill_ms_per_token == 0.5


def test_fit_needs_some_successful_requests() -> None:
    with pytest.raises(ValueError, match="not enough"):
        fit_profile([], STANDARD)


async def test_failed_requests_are_reported_not_fitted() -> None:
    clock = SimClock()
    server = _server(clock)
    calls = 0

    async def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 4:
            return httpx.Response(500, json={"error": "boom"})
        return await server._handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        run = await calibrate(
            CONFIG,
            PLAN,
            tiers=[Tier.STANDARD],
            clock=clock,
            client=client,
            run_id="t",
            measured_at="t",
            machine={},
        )

    profile = run.document["profiles"]["standard"]
    assert (profile["samples"], profile["failed_requests"]) == (23, 1)
    assert any("BackendError" in note and "HTTP 500" in note for note in profile["notes"])
    assert profile["prefill_ms_per_token"] == pytest.approx(0.25)


async def test_server_that_is_down_fails_the_calibration_at_the_warm_up() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(Exception, match="could not connect"):
            await calibrate(
                CONFIG,
                PLAN,
                tiers=[Tier.STANDARD],
                clock=SimClock(),
                client=client,
                run_id="t",
                measured_at="t",
                machine={},
            )


# --- simulator against measurement ---------------------------------------------------------


async def test_simulator_matches_a_server_that_behaves_like_the_cost_model_when_alone() -> None:
    # With no fixed overhead beyond one decode step, the server at concurrency 1 is exactly
    # what the simulator models: prefill, then one step per token.
    run, _ = await _calibrate(overhead_ms=TRUTH["decode_base_ms"])
    tier = run.tiers["standard"]

    for sample, (ttft_ms, total_ms) in zip(tier.samples, tier.simulated, strict=True):
        if sample.concurrency == 1:
            assert ttft_ms == pytest.approx(sample.ttft_ms)
            assert total_ms == pytest.approx(sample.total_ms)


async def test_error_report_exposes_where_the_simulator_diverges() -> None:
    run, _ = await _calibrate(overhead_ms=TRUTH["decode_base_ms"])
    tier = run.tiers["standard"]
    reported = run.document["profiles"]["standard"]["mape_percent"]

    assert reported == dataclasses.asdict(errors(tier.samples, tier.simulated))
    # The fake server prefills concurrent requests in parallel. The simulator prefills
    # them one after another, so it predicts a later first token for all but the first.
    # That is a real modelling difference and the error report must show it, not hide it.
    together = [
        (s, sim) for s, sim in zip(tier.samples, tier.simulated, strict=True) if s.concurrency == 4
    ]
    assert any(sim[0] > s.ttft_ms * 1.05 for s, sim in together)
    assert reported["ttft_fit"] > 1.0
    assert reported["ttft_holdout"] == pytest.approx(reported["ttft_fit"])
    # Total latency is dominated by decoding, which the fit captures, so it is closer.
    assert reported["total_fit"] < reported["ttft_fit"]


async def test_fixed_overhead_is_fitted_and_no_longer_shows_up_as_ttft_error() -> None:
    exact, _ = await _calibrate(dataclasses.replace(PLAN, concurrency=(1,)), overhead_ms=20.0)
    slow_start, _ = await _calibrate(dataclasses.replace(PLAN, concurrency=(1,)), overhead_ms=200.0)

    assert exact.document["profiles"]["standard"]["per_request_overhead_ms"] == pytest.approx(0)
    assert exact.document["profiles"]["standard"]["mape_percent"]["ttft_fit"] == pytest.approx(0)
    # A constant start-up cost is the intercept of the TTFT fit, less one decode step.
    profile = slow_start.document["profiles"]["standard"]
    assert profile["prefill_ms_per_token"] == pytest.approx(0.25)
    assert profile["fit"]["ttft_vs_prompt_tokens"]["intercept"] == pytest.approx(200.0)
    assert profile["per_request_overhead_ms"] == pytest.approx(180.0)
    assert profile["mape_percent"]["ttft_fit"] == pytest.approx(0, abs=1e-6)


async def test_an_overhead_that_fits_negative_is_set_to_zero_and_flagged() -> None:
    # This server's first token comes straight after prefill, a whole decode step
    # sooner than the simulator can produce one.
    run, _ = await _calibrate(dataclasses.replace(PLAN, concurrency=(1,)), overhead_ms=0.0)
    profile = run.document["profiles"]["standard"]

    assert profile["per_request_overhead_ms"] == 0.0
    assert any("per_request_overhead_ms was -20" in note for note in profile["notes"])
    assert profile["mape_percent"]["ttft_fit"] > 1.0


async def test_errors_are_also_reported_by_concurrency() -> None:
    run, _ = await _calibrate(overhead_ms=120.0)
    tier = run.tiers["standard"]
    by_level = run.document["profiles"]["standard"]["mape_percent_by_concurrency"]

    assert by_level == errors_by_concurrency(tier.samples, tier.simulated)
    assert list(by_level) == ["1", "2", "4"]
    assert [by_level[level]["requests"] for level in by_level] == [12, 4, 8]
    # Alone, this server is exactly what the simulator models.
    assert by_level["1"]["ttft"] == pytest.approx(0, abs=1e-6)
    assert by_level["1"]["total"] == pytest.approx(0, abs=1e-6)
    # Together it prefills in parallel and the simulator does not: the error is shown.
    assert by_level["4"]["ttft"] > 1.0


async def test_repeats_of_an_exact_server_fit_the_same_values() -> None:
    run, _ = await _calibrate(dataclasses.replace(PLAN, repeats=3), overhead_ms=120.0)
    spread = run.document["profiles"]["standard"]["spread_across_repeats"]

    assert set(spread) == {
        "prefill_ms_per_token",
        "decode_base_ms",
        "decode_ms_per_extra_seq",
        "per_request_overhead_ms",
    }
    assert spread["decode_base_ms"]["values"] == pytest.approx([20.0, 20.0, 20.0])
    assert spread["per_request_overhead_ms"]["mean"] == pytest.approx(100.0)
    assert spread["decode_ms_per_extra_seq"]["range_percent_of_mean"] == pytest.approx(0, abs=1e-6)


def test_spread_across_repeats_shows_how_far_apart_the_repeats_are() -> None:
    def repeat(number: int, per_token_ms: float) -> list[Sample]:
        return [
            _sample(repeat=number, prompt_tokens=10, ttft_ms=100.0, per_token_ms=per_token_ms),
            _sample(repeat=number, prompt_tokens=110, ttft_ms=150.0, per_token_ms=per_token_ms),
            _sample(
                repeat=number, sweep="concurrency", concurrency=3, per_token_ms=per_token_ms + 6
            ),
        ]

    spread = spread_across_repeats([*repeat(0, 18.0), *repeat(1, 22.0)], STANDARD)

    assert spread is not None
    assert spread["decode_base_ms"]["values"] == pytest.approx([18.0, 22.0])
    assert spread["decode_base_ms"]["mean"] == pytest.approx(20.0)
    assert spread["decode_base_ms"]["range_percent_of_mean"] == pytest.approx(20.0)
    assert spread["decode_ms_per_extra_seq"]["values"] == pytest.approx([3.0, 3.0])
    assert spread["prefill_ms_per_token"]["values"] == pytest.approx([0.5, 0.5])
    # TTFT intercept 95 ms, less the decode step of each repeat.
    assert spread["per_request_overhead_ms"]["values"] == pytest.approx([77.0, 73.0])
    assert spread_across_repeats(repeat(0, 18.0), STANDARD) is None


def test_spread_across_runs_compares_calibration_files() -> None:
    def document(base_ms: float, overhead_ms: float | None) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "prefill_ms_per_token": 0.5,
            "decode_base_ms": base_ms,
            "decode_ms_per_extra_seq": 0.0,
        }
        if overhead_ms is not None:
            profile["per_request_overhead_ms"] = overhead_ms
        return {"profiles": {"economy": profile}}

    spread = spread_across_runs([document(9.0, 90.0), document(11.0, 110.0), document(10.0, None)])

    assert spread["economy"]["decode_base_ms"]["values"] == [9.0, 11.0, 10.0]
    assert spread["economy"]["decode_base_ms"]["range_percent_of_mean"] == pytest.approx(20.0)
    # A file from before the overhead was fitted counts as 0, which the spread shows.
    assert spread["economy"]["per_request_overhead_ms"]["values"] == [90.0, 110.0, 0.0]
    # A parameter that is 0 in every run has no relative spread.
    assert spread["economy"]["decode_ms_per_extra_seq"]["range_percent_of_mean"] is None
    text = spread_summary(spread)
    assert "economy   decode_base_ms" in text
    assert "20.0%" in text


def test_compare_option_prints_the_spread_without_measuring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = []
    for index, base_ms in enumerate((9.0, 11.0)):
        profile = {
            "prefill_ms_per_token": 0.5,
            "decode_base_ms": base_ms,
            "decode_ms_per_extra_seq": 1.0,
            "per_request_overhead_ms": 100.0,
        }
        path = tmp_path / f"run{index}.json"
        path.write_text(
            json.dumps({"measured_at": f"day {index}", "profiles": {"premium": profile}}),
            encoding="utf-8",
        )
        paths.append(str(path))
    monkeypatch.setattr(sys, "argv", ["calibrate", "--compare", *paths])

    calibrate_module.main()

    out = capsys.readouterr().out
    assert "fitted parameters of 2 calibration runs" in out
    assert "measured at day 1" in out
    assert "premium   decode_base_ms" in out
    assert "9.0000, 11.0000" in out


async def test_simulate_replays_requests_with_the_measured_token_counts() -> None:
    samples = [
        _sample(prompt_tokens=40, completion_tokens=5, concurrency=1),
        _sample(prompt_tokens=8, completion_tokens=3, concurrency=1, repeat=1),
    ]
    profile = dataclasses.replace(
        STANDARD, prefill_ms_per_token=0.5, decode_base_ms=10.0, decode_ms_per_extra_seq=0.0
    )
    assert await simulate(samples, profile) == [(30.0, 70.0), (14.0, 34.0)]


# --- outputs -------------------------------------------------------------------------------


async def test_outputs_are_written_with_hardware_and_plots(tmp_path: Path) -> None:
    run, _ = await _calibrate()
    out = tmp_path / "calibration"

    write_outputs(run, out)

    document = json.loads((out / "calibrated_profiles.json").read_text(encoding="utf-8"))
    assert document == json.loads(json.dumps(run.document))
    assert document["schema"] == 3
    assert document["hardware"] == {"cpu": "test"}
    assert document["measured_at"] == "2026-01-01T00:00:00+00:00"
    assert document["notes"] == "unit test"
    assert document["plan"]["repeats"] == 2
    assert "do not transfer to other hardware" in document["what_this_is"]
    # The fake server answers neither as Ollama nor as llama-server.
    assert document["servers"]["standard"] == {
        "url": "http://127.0.0.1:9000",
        "kind": "unknown",
        "version": "unknown",
        "model": "unknown",
        "quantization": "unknown",
        # This fake server has no process list, so where the model ran is unknown.
        "placement": None,
    }
    samples = json.loads((out / "samples.json").read_text(encoding="utf-8"))
    assert len(samples) == 24
    assert set(samples[0]) == {f.name for f in dataclasses.fields(Sample)}
    plot = (out / "standard.png").read_bytes()
    assert plot.startswith(b"\x89PNG")
    assert len(plot) > 20_000
    assert sorted(p.name for p in out.iterdir()) == [
        "calibrated_profiles.json",
        "samples.json",
        "standard.png",
    ]


async def test_summary_prints_parameters_errors_and_notes() -> None:
    run, _ = await _calibrate(dataclasses.replace(PLAN, concurrency=(1,)))
    text = summary(run.document)
    lines = text.splitlines()

    assert "MAPE" in lines[0]
    assert lines[1].startswith("standard")
    assert "0.2500" in lines[1]
    assert "20.000" in lines[1]
    assert "note: only one concurrency level" in text


def test_machine_description_has_the_fields_the_docs_promise() -> None:
    machine = describe_machine()
    assert set(machine) == {"cpu", "logical_cores", "ram_gb", "gpu", "os", "machine", "python"}
    assert machine["cpu"]
    assert machine["logical_cores"] >= 1
    assert machine["ram_gb"] is None or machine["ram_gb"] > 0


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        (
            {
                "/api/version": {"version": "0.12.6"},
                "/api/show": {
                    "details": {"quantization_level": "Q4_K_M", "parameter_size": "3.2B"}
                },
            },
            {"kind": "ollama", "version": "0.12.6", "model": "m", "quantization": "Q4_K_M"},
        ),
        (
            {"/props": {"build_info": "b6000-abc", "model_path": "/m/Llama-3.2-1B-Q4_K_M.gguf"}},
            {
                "kind": "llama-server",
                "version": "b6000-abc",
                "model": "Llama-3.2-1B-Q4_K_M.gguf",
                "quantization": "Q4_K_M",
            },
        ),
        ({}, {"kind": "unknown", "version": "unknown", "quantization": "unknown"}),
    ],
    ids=["ollama", "llama-server", "unknown"],
)
async def test_server_description(responses: dict[str, Any], expected: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = responses.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        info = await describe_server(client, LlamaServer("http://h:1", "m"))

    assert {key: info[key] for key in expected} == expected
    assert info["url"] == "http://h:1"


# --- loading calibrated profiles into the simulator ----------------------------------------


async def test_simulator_runs_on_profiles_loaded_from_a_calibration_file(tmp_path: Path) -> None:
    run, _ = await _calibrate()
    write_outputs(run, tmp_path)

    profiles = load_calibrated_profiles(tmp_path / "calibrated_profiles.json")

    # Only the calibrated tier changed, and only its timing parameters.
    assert profiles[Tier.ECONOMY] == PROFILES[Tier.ECONOMY]
    assert profiles[Tier.PREMIUM] == PROFILES[Tier.PREMIUM]
    standard = profiles[Tier.STANDARD]
    assert standard.prefill_ms_per_token == pytest.approx(0.25)
    assert standard.decode_base_ms == pytest.approx(20.0)
    assert standard.decode_ms_per_extra_seq == pytest.approx(3.0)
    assert standard.max_batch_size == STANDARD.max_batch_size

    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7, prefix_cache=False, profiles=profiles)
    result = await registry.submit(GenerateRequest("r1", "standard", "one two three four", 5))
    # 4 prompt tokens at 0.25 ms, then 5 decode steps of 20 ms.
    assert result.time_to_first_token_ms == pytest.approx(21.0)
    assert result.latency_ms == pytest.approx(101.0)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "no 'profiles' section"),
        ('{"profiles": {}}', "no 'profiles' section"),
        ('{"profiles": {"ultra": {}}}', "'ultra' is not a tier"),
        ('{"profiles": {"economy": {"decode_base_ms": 5}}}', "prefill_ms_per_token must be a"),
        (
            '{"profiles": {"economy": {"prefill_ms_per_token": "fast", "decode_base_ms": 5, '
            '"decode_ms_per_extra_seq": 1}}}',
            "prefill_ms_per_token must be a number",
        ),
        (
            '{"profiles": {"economy": {"prefill_ms_per_token": 0.1, "decode_base_ms": 0, '
            '"decode_ms_per_extra_seq": 1}}}',
            "decode_base_ms must be positive",
        ),
    ],
)
def test_bad_calibration_files_are_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "calibrated_profiles.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_calibrated_profiles(path)


def test_calibration_file_overhead_is_optional_and_checked(tmp_path: Path) -> None:
    timing = {"prefill_ms_per_token": 0.5, "decode_base_ms": 5, "decode_ms_per_extra_seq": 1}
    path = tmp_path / "calibrated_profiles.json"

    path.write_text(json.dumps({"profiles": {"economy": timing}}), encoding="utf-8")
    assert load_calibrated_profiles(path)[Tier.ECONOMY].per_request_overhead_ms == 0.0

    with_overhead = {**timing, "per_request_overhead_ms": 90}
    path.write_text(json.dumps({"profiles": {"economy": with_overhead}}), encoding="utf-8")
    assert load_calibrated_profiles(path)[Tier.ECONOMY].per_request_overhead_ms == 90.0

    bad = {**timing, "per_request_overhead_ms": "slow"}
    path.write_text(json.dumps({"profiles": {"economy": bad}}), encoding="utf-8")
    with pytest.raises(ValueError, match="per_request_overhead_ms must be a number"):
        load_calibrated_profiles(path)


def test_backends_file_can_name_a_calibration_file() -> None:
    config = parse_backend_config({"simulated": {"profiles": "some/dir/calibrated_profiles.json"}})
    assert config.profiles_path == Path("some/dir/calibrated_profiles.json")
    assert parse_backend_config({}).profiles_path is None
    with pytest.raises(BackendConfigError, match=r"simulated\.profiles"):
        parse_backend_config({"simulated": {"profiles": 3}})
    with pytest.raises(BackendConfigError, match="'simulated' must be a mapping"):
        parse_backend_config({"simulated": ["x"]})


async def test_default_app_loads_calibrated_profiles_by_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = await _calibrate()
    write_outputs(run, tmp_path)
    monkeypatch.delenv("GATEWAY_BACKEND", raising=False)
    monkeypatch.delenv("GATEWAY_BACKENDS", raising=False)

    monkeypatch.setenv("GATEWAY_PROFILES", str(tmp_path / "calibrated_profiles.json"))
    transport = httpx.ASGITransport(app=create_default_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        ok = await client.get("/healthz")
    assert ok.status_code == 200

    # The setting is really read: a file that is not there stops the server from starting.
    monkeypatch.setenv("GATEWAY_PROFILES", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        create_default_app()


def test_calibrate_module_does_not_need_a_running_event_loop_to_import() -> None:
    assert asyncio.iscoroutinefunction(calibrate)


# --- where the model was loaded -------------------------------------------------------------

OFFLOADED = {
    "source": "ollama /api/ps",
    "size_bytes": 8000,
    "size_vram_bytes": 7600,
    "gpu_percent": 95.0,
    "cpu_percent": 5.0,
}


def _process_list_client(models: object) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": models})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_placement_is_read_from_the_servers_process_list() -> None:
    models = [
        {"name": "another:1b", "size": 10, "size_vram": 10},
        {"name": "llama3.1:8b", "model": "llama3.1:8b", "size": 8000, "size_vram": 7600},
    ]
    async with _process_list_client(models) as client:
        placement = await describe_placement(
            client, LlamaServer("http://127.0.0.1:11434", "llama3.1:8b")
        )
        other = await describe_placement(
            client, LlamaServer("http://127.0.0.1:11434", "another:1b")
        )

    assert placement == OFFLOADED
    assert other is not None
    assert (other["gpu_percent"], other["cpu_percent"]) == (100.0, 0.0)


def test_a_model_partly_on_the_cpu_gets_the_hardware_limited_note() -> None:
    on_gpu = {**OFFLOADED, "size_vram_bytes": 8000, "gpu_percent": 100.0, "cpu_percent": 0.0}

    assert hardware_limited_note([on_gpu]) is None
    note = hardware_limited_note([on_gpu, OFFLOADED])
    assert note is not None
    assert note.startswith("hardware-limited: the model did not fit on the GPU.")
    assert "5% of it on the CPU" in note


@pytest.mark.parametrize(
    "models",
    [
        [],
        [{"name": "llama3.1:8b"}],
        [{"name": "llama3.1:8b", "size": 0, "size_vram": 0}],
        [{"name": "llama3.1:8b", "size": "big", "size_vram": 1}],
        "nonsense",
    ],
)
async def test_placement_is_unknown_when_the_server_does_not_say(models: object) -> None:
    async with _process_list_client(models) as client:
        assert (
            await describe_placement(client, LlamaServer("http://127.0.0.1:11434", "llama3.1:8b"))
            is None
        )
        # A tier with no model name cannot be looked up in the list.
        assert await describe_placement(client, LlamaServer("http://127.0.0.1:11434")) is None


async def test_calibration_records_placement_and_flags_a_hardware_limited_tier() -> None:
    clock = SimClock()
    server = _server(clock, overhead_ms=120.0)

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"name": "big", "size": 8000, "size_vram": 7600}]}
            )
        return await server._handle(request)

    config = BackendConfig(
        kind="llamacpp",
        servers={tier: LlamaServer("http://127.0.0.1:9000", "big") for tier in Tier},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        run = await calibrate(
            config,
            PLAN,
            tiers=[Tier.STANDARD],
            clock=clock,
            client=client,
            run_id="test",
            measured_at="2026-01-01T00:00:00+00:00",
            machine={"cpu": "test"},
        )

    profile = run.document["profiles"]["standard"]
    assert run.document["servers"]["standard"]["placement"] == OFFLOADED
    assert profile["hardware_limited"] is True
    assert [note for note in profile["notes"] if note.startswith("hardware-limited")]
    # The fit itself is untouched by the flag.
    assert profile["decode_base_ms"] == pytest.approx(20.0)


async def test_placement_that_was_not_reported_is_unknown_not_assumed() -> None:
    run, _ = await _calibrate(overhead_ms=120.0)

    assert run.document["servers"]["standard"]["placement"] is None
    assert run.document["profiles"]["standard"]["hardware_limited"] is None
    assert run.document["profiles"]["standard"]["notes"] == []


# --- combining saved runs -------------------------------------------------------------------


async def _saved_runs(folder: Path, extras: tuple[float, ...]) -> list[Path]:
    """One saved run per value: the same fake server except for its cost per extra sequence."""
    paths = []
    for index, extra_ms in enumerate(extras, start=1):
        run, _ = await _calibrate(overhead_ms=120.0, decode_ms_per_extra_seq=extra_ms)
        paths.append(folder / f"run{index}")
        write_outputs(run, paths[-1])
    return paths


def test_median_takes_the_middle_value_or_the_mean_of_the_middle_two() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
    assert median([7.0]) == 7.0


def test_a_parameter_is_unstable_only_above_a_quarter_of_its_median() -> None:
    steady = _combine([90.0, 100.0, 115.0])
    assert (steady["median"], steady["min"], steady["max"]) == (100.0, 90.0, 115.0)
    assert steady["range_percent_of_median"] == 25.0
    assert steady["unstable"] is False

    assert _combine([90.0, 100.0, 116.0])["unstable"] is True
    # All zero: there is no relative spread to judge.
    nothing = _combine([0.0, 0.0])
    assert (nothing["range_percent_of_median"], nothing["unstable"]) == (None, False)


async def test_aggregate_reports_the_median_with_its_range_and_flags_what_did_not_repeat(
    tmp_path: Path,
) -> None:
    paths = await _saved_runs(tmp_path, (2.0, 10.0, 3.0))

    combined = await aggregate([(path.name, *load_run(path)) for path in paths])
    document = combined.document
    profile = document["profiles"]["standard"]

    # The median: not the mean (5), and not whichever run looks best.
    assert profile["decode_ms_per_extra_seq"] == pytest.approx(3.0)
    extra = profile["aggregate"]["decode_ms_per_extra_seq"]
    assert (extra["min"], extra["max"]) == (pytest.approx(2.0), pytest.approx(10.0))
    assert extra["values"] == pytest.approx([2.0, 10.0, 3.0])
    assert extra["range_percent_of_median"] == pytest.approx(800 / 3)
    assert extra["unstable"] is True
    assert profile["unstable_parameters"] == ["decode_ms_per_extra_seq"]
    assert profile["aggregate"]["decode_base_ms"]["unstable"] is False
    assert profile["decode_base_ms"] == pytest.approx(20.0)
    assert profile["per_request_overhead_ms"] == pytest.approx(100.0)
    assert any(
        note.startswith("unstable: decode_ms_per_extra_seq ranged from 2 to 10 over 3 runs")
        for note in profile["notes"]
    )

    assert [r["run"] for r in document["aggregated_from"]] == ["run1", "run2", "run3"]
    assert "no run was preferred or dropped" in document["notes"]
    assert document["unstable_above_range_percent"] == 25.0
    assert profile["samples"] == 72
    assert profile["hardware_limited"] is None
    assert profile["fit_r2_by_run"]["ttft_vs_prompt_tokens"] == pytest.approx([1.0, 1.0, 1.0])
    # Alone, every run's server is what the median profile describes; together they differ.
    by_level = profile["mape_percent_by_concurrency"]
    assert by_level["1"]["ttft"] == pytest.approx(0, abs=1e-6)
    assert by_level["4"]["total"] > 1.0
    assert len(combined.tiers["standard"].samples) == 72


async def test_aggregate_is_written_without_samples_and_loads_into_the_simulator(
    tmp_path: Path,
) -> None:
    paths = await _saved_runs(tmp_path, (2.0, 4.0))
    out = tmp_path / "combined"

    write_outputs(await aggregate([(p.name, *load_run(p)) for p in paths]), out, samples=False)

    assert sorted(p.name for p in out.iterdir()) == ["calibrated_profiles.json", "standard.png"]
    loaded = load_calibrated_profiles(out / "calibrated_profiles.json")[Tier.STANDARD]
    # Two runs: the median is midway between them.
    assert loaded.decode_ms_per_extra_seq == pytest.approx(3.0)
    assert loaded.per_request_overhead_ms == pytest.approx(100.0)
    # An aggregate is not a run: it cannot be fed back in.
    with pytest.raises(ValueError, match="holds an aggregate"):
        load_run(out)


async def test_aggregate_flags_a_tier_that_was_partly_on_the_cpu_in_any_run(
    tmp_path: Path,
) -> None:
    loaded = [(p.name, *load_run(p)) for p in await _saved_runs(tmp_path, (3.0, 3.0))]
    loaded[1][1]["servers"]["standard"]["placement"] = OFFLOADED

    profile = (await aggregate(loaded)).document["profiles"]["standard"]

    assert profile["hardware_limited"] is True
    assert profile["placement_by_run"] == {"run1": None, "run2": OFFLOADED}
    assert [note for note in profile["notes"] if note.startswith("hardware-limited")]
    assert profile["unstable_parameters"] == []


async def test_aggregate_needs_at_least_two_runs(tmp_path: Path) -> None:
    (path,) = await _saved_runs(tmp_path, (3.0,))
    with pytest.raises(ValueError, match="at least two runs"):
        await aggregate([(path.name, *load_run(path))])


def test_aggregate_option_combines_saved_runs_without_measuring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not an async test: main() starts an event loop of its own.
    paths = [str(p) for p in asyncio.run(_saved_runs(tmp_path, (2.0, 10.0, 3.0)))]
    out = tmp_path / "combined"

    monkeypatch.setattr(sys, "argv", ["calibrate", "--aggregate", *paths, "--out", str(out)])
    calibrate_module.main()

    text = capsys.readouterr().out
    assert "median of 3 calibration runs. Nothing was re-measured." in text
    assert "standard  decode_ms_per_extra_seq" in text
    assert "UNSTABLE" in text
    assert "note: unstable: decode_ms_per_extra_seq" in text
    assert (out / "calibrated_profiles.json").exists()

    # Writing the aggregate over one of the runs it was made from is refused.
    monkeypatch.setattr(sys, "argv", ["calibrate", "--aggregate", *paths, "--out", paths[0]])
    with pytest.raises(SystemExit):
        calibrate_module.main()
