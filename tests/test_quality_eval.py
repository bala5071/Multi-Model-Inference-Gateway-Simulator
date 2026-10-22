from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from benchmarks.quality_eval import (
    DATASET_ROWS,
    MAX_TOKENS,
    QUALITY,
    QUESTIONS,
    SAMPLE_SIZE,
    Answer,
    Question,
    build_prompt,
    evaluate,
    fetch_sample,
    load_questions,
    paired_difference,
    parse_choice,
    sample_rows,
    summarise,
    summary,
    wilson_interval,
)

from gateway.backend_config import BackendConfig, LlamaServer
from gateway.clock import SimClock
from gateway.profiles import Tier
from mock_llama import PacedStream, sse

PORTS = {Tier.ECONOMY: 9001, Tier.STANDARD: 9002, Tier.PREMIUM: 9003}
CONFIG = BackendConfig(
    kind="llamacpp",
    servers={tier: LlamaServer(f"http://127.0.0.1:{port}") for tier, port in PORTS.items()},
)
QUESTION = Question(
    id="q1",
    subject="high_school_physics",
    question="What is the SI unit of force?",
    choices=("joule", "newton", "watt", "pascal"),
    answer="B",
)


def _questions(golds: str) -> list[Question]:
    return [
        Question(f"q{i}", "test_subject", f"Question {i}?", ("w", "x", "y", "z"), gold)
        for i, gold in enumerate(golds)
    ]


class FakeModels:
    """Model servers that answer by rule: one rule per port, given the prompt."""

    def __init__(self, clock: SimClock, replies: dict[int, Callable[[str], str | None]]) -> None:
        self.clock = clock
        self.replies = replies
        self.bodies: list[dict[str, Any]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)  # no version, no process list: an unknown server
        body = json.loads(request.content)
        self.bodies.append(body)
        reply = self.replies[request.url.port or 80](body["prompt"])
        if reply is None:
            return httpx.Response(500, json={"error": {"message": "broken"}})
        usage = {"prompt_tokens": 50, "completion_tokens": 1}
        chunks = [
            (40.0, sse({"choices": [{"index": 0, "text": reply}]})),
            (0.0, sse({"choices": [], "usage": usage})),
            (0.0, sse("[DONE]")),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=PacedStream(self.clock, chunks),
        )


async def _evaluate(
    questions: list[Question], replies: dict[Tier, Callable[[str], str | None]]
) -> tuple[dict[str, Any], FakeModels]:
    clock = SimClock()
    models = FakeModels(clock, {PORTS[tier]: rule for tier, rule in replies.items()})
    async with models.client() as client:
        document = await evaluate(
            CONFIG,
            {"name": "test set", "license": "MIT"},
            questions,
            tiers=list(replies),
            clock=clock,
            client=client,
            measured_at="2026-01-01T00:00:00+00:00",
            machine={"cpu": "test"},
            notes="unit test",
        )
    return document, models


# --- the question set ----------------------------------------------------------------------


def test_sample_is_a_fixed_hundred_distinct_rows() -> None:
    rows = sample_rows()
    assert rows == sample_rows()
    assert len(rows) == len(set(rows)) == SAMPLE_SIZE == 100
    assert rows == sorted(rows)
    assert rows[0] >= 0
    assert rows[-1] < DATASET_ROWS


def test_tracked_question_file_is_the_sample_and_states_its_license() -> None:
    source, questions = load_questions(QUESTIONS)

    assert [q.id for q in questions] == [f"mmlu-test-{row}" for row in sample_rows()]
    assert all(len(q.choices) == 4 and q.answer in "ABCD" and q.question for q in questions)
    assert source["license"] == "MIT"
    assert source["dataset"] == "https://huggingface.co/datasets/cais/mmlu"
    license_text = (QUALITY / "LICENSE-MMLU.txt").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert source["copyright"] in license_text


@pytest.mark.parametrize(
    "item",
    [
        {"id": "x", "subject": "s", "question": "q", "choices": ["a", "b", "c"], "answer": "A"},
        {
            "id": "x",
            "subject": "s",
            "question": "q",
            "choices": ["a", "b", "c", "d"],
            "answer": "E",
        },
    ],
)
def test_question_files_with_malformed_items_are_rejected(
    tmp_path: Path, item: dict[str, Any]
) -> None:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps({"source": {}, "items": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a four-option question"):
        load_questions(path)

    path.write_text(json.dumps({"source": {}, "items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="has no questions"):
        load_questions(path)


def _rows_server(
    clock: SimClock, *, total: int = DATASET_ROWS, refuse_first: int = 0
) -> tuple[httpx.AsyncClient, list[int]]:
    asked: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        asked.append(offset)
        if len(asked) <= refuse_first:
            return httpx.Response(429)
        row = {
            "question": f"Question {offset}?",
            "subject": "anatomy",
            "choices": ["a", "b", "c", "d"],
            "answer": offset % 4,
        }
        page = {"rows": [{"row_idx": offset, "row": row}], "num_rows_total": total}
        return httpx.Response(200, json=page)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), asked


async def test_fetch_downloads_exactly_the_sampled_rows_and_records_the_source() -> None:
    clock = SimClock()
    client, asked = _rows_server(clock, refuse_first=2)
    async with client:
        sample = await fetch_sample(client, clock, "2026-01-01T00:00:00+00:00")

    rows = sample_rows()
    # The first row was refused twice (429) and asked for again after waiting.
    assert asked == [rows[0], rows[0], *rows]
    assert clock.now_ms() >= 5_000.0 + 10_000.0
    assert [item["id"] for item in sample["items"]] == [f"mmlu-test-{row}" for row in rows]
    assert sample["items"][0]["answer"] == "ABCD"[rows[0] % 4]
    assert sample["source"]["license"] == "MIT"
    assert "random.Random(7).sample(range(14042), 100)" in sample["source"]["selection"]


async def test_fetch_refuses_a_dataset_that_has_changed_size() -> None:
    clock = SimClock()
    client, _ = _rows_server(clock, total=DATASET_ROWS + 1)
    async with client:
        with pytest.raises(ValueError, match="would not be the same questions"):
            await fetch_sample(client, clock, "now")


# --- the prompt and the scoring --------------------------------------------------------------


def test_prompt_is_the_fixed_template() -> None:
    assert build_prompt(QUESTION) == (
        "The following is a multiple choice question about high school physics.\n"
        "\n"
        "What is the SI unit of force?\n"
        "A. joule\n"
        "B. newton\n"
        "C. watt\n"
        "D. pascal\n"
        "\n"
        "Reply with the letter of the correct option only: A, B, C or D.\n"
        "Answer:"
    )


@pytest.mark.parametrize(
    ("reply", "chosen"),
    [
        ("B", "B"),
        (" B", "B"),
        ("\nC.", "C"),
        ("(D)", "D"),
        ("A) joule", "A"),
        ("B. newton", "B"),
        # Not an option letter at the start: no exact match.
        ("The answer is B", None),
        ("Answer: B", None),
        ("b", None),
        ("Both A and B", None),
        ("E", None),
        ("", None),
    ],
)
def test_only_a_reply_that_opens_with_an_option_letter_counts(
    reply: str, chosen: str | None
) -> None:
    assert parse_choice(reply) == chosen


def test_wilson_interval_matches_known_values() -> None:
    assert wilson_interval(50, 100) == pytest.approx((0.40383, 0.59617), abs=1e-5)
    assert wilson_interval(90, 100) == pytest.approx((0.82563, 0.94477), abs=1e-5)
    # At the edges it stays inside 0..1 and does not collapse to a point.
    assert wilson_interval(0, 10) == pytest.approx((0.0, 0.27753), abs=1e-5)
    assert wilson_interval(10, 10) == pytest.approx((0.72247, 1.0), abs=1e-5)
    assert wilson_interval(10, 10)[1] <= 1.0


@pytest.mark.parametrize(("correct", "total"), [(1, 0), (-1, 5), (6, 5)])
def test_wilson_interval_rejects_impossible_counts(correct: int, total: int) -> None:
    with pytest.raises(ValueError, match="need 0 <= correct"):
        wilson_interval(correct, total)


def test_paired_difference_uses_only_the_questions_the_two_disagree_on() -> None:
    # Ten questions: both right on 5, only the first right on 3, only the second on 1.
    first = [True] * 5 + [True] * 3 + [False] * 1 + [False]
    second = [True] * 5 + [False] * 3 + [True] * 1 + [False]

    d = paired_difference(first, second)

    assert d["difference"] == pytest.approx(0.2)
    assert (d["only_first_correct"], d["only_second_correct"]) == (3, 1)
    # 1.959964 * sqrt(3 + 1 - 2**2 / 10) / 10
    assert d["high"] - d["difference"] == pytest.approx(0.37188, abs=1e-5)
    assert d["low"] == pytest.approx(0.2 - 0.37188, abs=1e-5)

    same = paired_difference([True, False], [True, False])
    assert (same["difference"], same["low"], same["high"]) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="same, non-zero number"):
        paired_difference([True], [True, False])


# --- running it ------------------------------------------------------------------------------


async def test_each_tier_is_scored_by_exact_match_on_the_same_questions() -> None:
    questions = _questions("ABCDABCDAB")

    def always_b(prompt: str) -> str:
        return " B"

    def knows_it(prompt: str) -> str:
        if "Question" not in prompt:
            return "A"  # the warm-up
        number = int(prompt.split("Question ")[1].split("?")[0])
        # Right every time, but on question 3 it explains instead of answering.
        return "The answer is D" if number == 3 else f"{questions[number].answer}."

    document, models = await _evaluate(questions, {Tier.ECONOMY: always_b, Tier.PREMIUM: knows_it})

    economy, premium = document["tiers"]["economy"], document["tiers"]["premium"]
    assert (economy["questions"], economy["correct"], economy["accuracy"]) == (10, 3, 0.3)
    assert (economy["unparsed"], economy["failed"]) == (0, 0)
    assert (premium["correct"], premium["unparsed"]) == (9, 1)
    assert economy["ci95"]["low"] < 0.3 < economy["ci95"]["high"]
    assert economy["ci95"]["method"] == "Wilson score"
    # Every reply arrived 40 ms after it was asked for.
    assert economy["latency_ms"]["total_p99"] == pytest.approx(40.0)
    assert economy["mean_prompt_tokens"] == 50
    assert economy["cost_per_1000_requests_usd"] == pytest.approx(
        1000 * (50 * 0.0001 + 0.0002) / 1000
    )

    answers = document["answers"]["premium"]
    assert [a["id"] for a in answers] == [q.id for q in questions]
    assert answers[3] == {
        "id": "q3",
        "subject": "test_subject",
        "gold": "D",
        "output": "The answer is D",
        "chosen": None,
        "correct": False,
        "ttft_ms": pytest.approx(40.0),
        "total_ms": pytest.approx(40.0),
        "prompt_tokens": 50,
        "completion_tokens": 1,
        "cost_usd": pytest.approx((50 * 0.0010 + 0.0020) / 1000),
        "error": None,
    }
    difference = document["differences"]["premium_minus_economy"]
    assert difference["difference"] == pytest.approx(0.6)
    assert list(document["differences"]) == ["premium_minus_economy"]

    # One warm-up and ten questions per tier, all at temperature 0 with the fixed seed.
    assert len(models.bodies) == 22
    assert all(body["temperature"] == 0.0 and body["seed"] == 7 for body in models.bodies)
    assert {body["max_tokens"] for body in models.bodies[1:11]} == {MAX_TOKENS}
    assert models.bodies[1]["prompt"] == build_prompt(questions[0])


async def test_results_record_the_method_the_set_and_the_machine() -> None:
    document, _ = await _evaluate(_questions("AB"), {Tier.STANDARD: lambda prompt: "A"})

    assert document["schema"] == 1
    assert document["hardware"] == {"cpu": "test"}
    assert document["questions"] == {"name": "test set", "license": "MIT", "count": 2}
    assert document["method"]["temperature"] == 0.0
    assert document["method"]["runs"] == 1
    assert "exact match on the option letter" in document["method"]["scoring"]
    assert "{question}" in document["method"]["prompt_template"]
    assert document["servers"]["standard"]["kind"] == "unknown"
    # This server has no process list, so where the model ran is unknown, not assumed.
    assert document["servers"]["standard"]["placement"] is None
    assert document["tiers"]["standard"]["hardware_limited"] is None
    assert document["differences"] == {}
    assert "single run" in document["what_this_is"]


async def test_a_failed_request_is_a_wrong_answer_and_is_counted_as_failed() -> None:
    def breaks_on_one(prompt: str) -> str | None:
        return None if "Question 1?" in prompt else "A"

    document, _ = await _evaluate(_questions("AAA"), {Tier.STANDARD: breaks_on_one})

    tier = document["tiers"]["standard"]
    assert (tier["questions"], tier["correct"], tier["failed"], tier["unparsed"]) == (3, 2, 1, 0)
    assert tier["accuracy"] == pytest.approx(2 / 3)
    failed = document["answers"]["standard"][1]
    assert failed["correct"] is False
    assert "HTTP 500" in failed["error"]
    assert failed["total_ms"] is None


def test_summary_with_no_served_request_has_no_latency_or_cost() -> None:
    broken = Answer("q", "s", "A", "", None, False, None, None, 0, 0, 0.0, "BackendError: down")

    found = summarise([broken])

    assert (found["correct"], found["failed"]) == (0, 1)
    assert found["latency_ms"]["total_p99"] is None
    assert found["cost_per_1000_requests_usd"] is None


async def test_summary_prints_accuracy_intervals_and_differences() -> None:
    document, _ = await _evaluate(
        _questions("ABAB"), {Tier.ECONOMY: lambda p: "A", Tier.STANDARD: lambda p: "B"}
    )
    text = summary(document)
    lines = text.splitlines()

    assert "95% CI" in lines[0]
    assert lines[1].startswith("economy")
    assert "2/4" in lines[1]
    assert "50.0%" in lines[1]
    assert "standard minus economy: +0.0 points" in text
