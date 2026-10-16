"""Measure answer quality of the three tiers on a small fixed multiple-choice set.

    uv run python -m benchmarks.quality_eval --backends examples/backends.ollama.yaml

Each question in ``benchmarks/quality/mmlu_sample.json`` is sent once to every tier's real
model (see docs/12-real-models.md for starting one), through the gateway's own backend and
worker, at temperature 0 with one fixed prompt template. An answer is correct only if the
option letter the model gives is the right one. Writes ``benchmarks/quality/results.json``:
accuracy per tier with a 95% confidence interval, every answer, and the hardware and
servers it ran on. Everything in that file is measured, on one machine, in one run.

    uv run python -m benchmarks.quality_eval --fetch

downloads the question set again from the Hugging Face copy of MMLU (MIT license) and
rewrites the sample file. It asks no model anything.

    uv run python -m benchmarks.quality_eval --backends examples/backends.ollama.yaml --pilot

tries the candidate prompt templates on eight made-up questions, none of them from the
evaluation set. It is how the template was chosen. See docs/14-quality-eval.md.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import json
import math
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from benchmarks.calibrate import (
    describe_machine,
    describe_placement,
    describe_server,
    hardware_limited_note,
)
from gateway.backend_config import BackendConfig, build_llamacpp_registry, load_backend_config
from gateway.clock import Clock, WallClock
from gateway.metrics import percentile
from gateway.overload import run_with_timeout
from gateway.profiles import PROFILES, Tier
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest

SCHEMA_VERSION = 1
QUALITY = Path(__file__).resolve().parent / "quality"
QUESTIONS = QUALITY / "mmlu_sample.json"
RESULTS = QUALITY / "results.json"

# --- the question set ----------------------------------------------------------------------

DATASET = "cais/mmlu"
DATASET_CONFIG = "all"
DATASET_SPLIT = "test"
DATASET_ROWS = 14_042
SAMPLE_SIZE = 100
SAMPLE_SEED = 7
ROWS_API = "https://datasets-server.huggingface.co/rows"
LETTERS = ("A", "B", "C", "D")
FETCH_TRIES = 8
FETCH_PAUSE_MS = 500.0
FETCH_BACKOFF_MS = 5_000.0


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    subject: str
    question: str
    choices: tuple[str, str, str, str]
    # The letter of the correct option.
    answer: str


def sample_rows() -> list[int]:
    """Which rows of the test split make up the set: a fixed pseudo-random hundred."""
    return sorted(random.Random(SAMPLE_SEED).sample(range(DATASET_ROWS), SAMPLE_SIZE))


def load_questions(path: Path) -> tuple[dict[str, Any], list[Question]]:
    """The question file: where it came from, and the questions."""
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = []
    for item in data["items"]:
        choices = tuple(item["choices"])
        if len(choices) != len(LETTERS) or item["answer"] not in LETTERS:
            raise ValueError(f"{path}: {item['id']} is not a four-option question")
        questions.append(
            Question(
                id=item["id"],
                subject=item["subject"],
                question=item["question"],
                choices=(choices[0], choices[1], choices[2], choices[3]),
                answer=item["answer"],
            )
        )
    if not questions:
        raise ValueError(f"{path} has no questions")
    return data["source"], questions


async def fetch_sample(client: httpx.AsyncClient, clock: Clock, fetched_at: str) -> dict[str, Any]:
    """Download the sampled rows. Fails if the dataset no longer has the rows it had."""
    items = []
    for row_idx in sample_rows():
        params: dict[str, str | int] = {
            "dataset": DATASET,
            "config": DATASET_CONFIG,
            "split": DATASET_SPLIT,
            "offset": row_idx,
            "length": 1,
        }
        # The rows API limits how fast it may be asked and answers 5xx now and then:
        # go slowly, and wait longer after each refusal.
        wait_ms = FETCH_BACKOFF_MS
        for _ in range(FETCH_TRIES):
            await clock.sleep_ms(FETCH_PAUSE_MS)
            response = await client.get(ROWS_API, params=params)
            if response.status_code != 429 and response.status_code < 500:
                break
            await clock.sleep_ms(wait_ms)
            wait_ms *= 2
        response.raise_for_status()
        page = response.json()
        if page["num_rows_total"] != DATASET_ROWS:
            raise ValueError(
                f"{DATASET} {DATASET_SPLIT} has {page['num_rows_total']} rows, not "
                f"{DATASET_ROWS}: the sample would not be the same questions"
            )
        (entry,) = page["rows"]
        row = entry["row"]
        items.append(
            {
                "id": f"mmlu-{DATASET_SPLIT}-{entry['row_idx']}",
                "subject": row["subject"],
                "question": row["question"],
                "choices": list(row["choices"]),
                "answer": LETTERS[row["answer"]],
            }
        )
    return {
        "source": {
            "name": "MMLU (Measuring Massive Multitask Language Understanding)",
            "authors": "Hendrycks, Burns, Basart, Zou, Mazeika, Song, Steinhardt (2021)",
            "paper": "https://arxiv.org/abs/2009.03300",
            "dataset": f"https://huggingface.co/datasets/{DATASET}",
            "config": DATASET_CONFIG,
            "split": DATASET_SPLIT,
            "license": "MIT",
            "license_text": "benchmarks/quality/LICENSE-MMLU.txt",
            "copyright": "Copyright (c) 2020 Dan Hendrycks",
            "selection": (
                f"random.Random({SAMPLE_SEED}).sample(range({DATASET_ROWS}), {SAMPLE_SIZE}), "
                f"sorted, as row numbers of the {DATASET_SPLIT} split"
            ),
            "fetched_at": fetched_at,
            "fetched_from": ROWS_API,
        },
        "items": items,
    }


# --- asking and scoring --------------------------------------------------------------------

TEMPERATURE = 0.0
SAMPLING_SEED = 7
MAX_TOKENS = 8
# Candidate templates. Which one is used was settled with ``--pilot`` on the made-up
# questions below, before any question of the evaluation set was sent to a model.
TEMPLATES = {
    "instructed": """\
The following is a multiple choice question about {subject}.

{question}
A. {a}
B. {b}
C. {c}
D. {d}

Reply with the letter of the correct option only: A, B, C or D.
Answer:""",
    # The header MMLU is usually run with, without its worked examples.
    "mmlu": """\
The following are multiple choice questions (with answers) about {subject}.

{question}
A. {a}
B. {b}
C. {c}
D. {d}
Answer:""",
    "question_first": """\
Answer the following multiple choice question about {subject}. Reply with one letter only: \
the letter of the correct option.

Question: {question}

A) {a}
B) {b}
C) {c}
D) {d}""",
}
PROMPT_TEMPLATE = TEMPLATES["instructed"]

# The reply has to open with the option letter: "B", "B.", "(B)", "B) ...". Anything else,
# such as "The answer is B", is not an exact match and scores as wrong.
_CHOICE = re.compile(r"\s*\(?([ABCD])(?![A-Za-z])")


def build_prompt(question: Question, template: str = PROMPT_TEMPLATE) -> str:
    a, b, c, d = question.choices
    return template.format(
        subject=question.subject.replace("_", " "),
        question=question.question,
        a=a,
        b=b,
        c=c,
        d=d,
    )


def parse_choice(text: str) -> str | None:
    """The option letter a reply opens with, or None if it does not open with one."""
    found = _CHOICE.match(text)
    return found.group(1) if found else None


def wilson_interval(correct: int, total: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a proportion; 95% with the default ``z``."""
    if total < 1 or not 0 <= correct <= total:
        raise ValueError("need 0 <= correct <= total and total >= 1")
    p = correct / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    scale = 1 + z * z / total
    return max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale)


def paired_difference(
    first: Sequence[bool], second: Sequence[bool], z: float = 1.959964
) -> dict[str, float]:
    """Accuracy of ``first`` minus ``second`` on the same questions, with a Wald interval.

    The two were asked the same questions, so the interval uses only the questions they
    disagree on. With this few questions it is approximate.
    """
    if len(first) != len(second) or not first:
        raise ValueError("need the same, non-zero number of answers from both")
    n = len(first)
    only_first = sum(1 for a, b in zip(first, second, strict=True) if a and not b)
    only_second = sum(1 for a, b in zip(first, second, strict=True) if b and not a)
    difference = (only_first - only_second) / n
    spread = z * math.sqrt(only_first + only_second - (only_first - only_second) ** 2 / n) / n
    return {
        "difference": difference,
        "low": difference - spread,
        "high": difference + spread,
        "only_first_correct": only_first,
        "only_second_correct": only_second,
    }


# Written for this project to try templates on. None is from MMLU, and each option
# position is the right answer twice.
PILOT = (
    Question("pilot-1", "elementary_mathematics", "What is 7 + 5?", ("10", "11", "12", "13"), "C"),
    Question("pilot-2", "elementary_mathematics", "What is 9 - 4?", ("5", "3", "4", "6"), "A"),
    Question(
        "pilot-3",
        "geography",
        "Which city is the capital of France?",
        ("Rome", "Paris", "Madrid", "Berlin"),
        "B",
    ),
    Question(
        "pilot-4",
        "chemistry",
        "What is the chemical formula of water?",
        ("CO2", "O2", "NaCl", "H2O"),
        "D",
    ),
    Question(
        "pilot-5",
        "astronomy",
        "Which planet is known as the Red Planet?",
        ("Venus", "Jupiter", "Mars", "Saturn"),
        "C",
    ),
    Question(
        "pilot-6",
        "biology",
        "Which organ pumps blood around the human body?",
        ("the heart", "the liver", "the lungs", "the kidneys"),
        "A",
    ),
    Question(
        "pilot-7",
        "world_history",
        "In which year did the Second World War end?",
        ("1918", "1939", "1950", "1945"),
        "D",
    ),
    Question(
        "pilot-8",
        "computer_science",
        "How many bits are in one byte?",
        ("4", "8", "16", "32"),
        "B",
    ),
)


async def pilot(registry: ModelRegistry, tiers: Sequence[str]) -> list[dict[str, Any]]:
    """Every candidate template on the made-up questions: correct and unparsed, per tier."""
    rows = []
    for tier in tiers:
        worker = registry.get(tier)
        for name, template in TEMPLATES.items():
            replies = []
            for question in PILOT:
                prompt = build_prompt(question, template)
                result = await worker.submit(GenerateRequest(question.id, tier, prompt, MAX_TOKENS))
                replies.append((question.answer, parse_choice(result.text), result.text))
            rows.append(
                {
                    "tier": tier,
                    "template": name,
                    "questions": len(replies),
                    "correct": sum(1 for gold, chosen, _ in replies if chosen == gold),
                    "unparsed": sum(1 for _, chosen, _ in replies if chosen is None),
                    "replies": [text for _, _, text in replies],
                }
            )
    return rows


def pilot_summary(rows: Sequence[dict[str, Any]]) -> str:
    lines = [f"{'tier':9} {'template':15} {'correct':>8} {'unparsed':>8}  replies"]
    lines.extend(
        f"{r['tier']:9} {r['template']:15} {r['correct']:4d}/{r['questions']:<3d} "
        f"{r['unparsed']:8d}  " + " | ".join(repr(text.strip()[:14]) for text in r["replies"])
        for r in rows
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Answer:
    """One question put to one tier."""

    id: str
    subject: str
    gold: str
    # What the model wrote; empty if the request failed.
    output: str
    chosen: str | None
    correct: bool
    ttft_ms: float | None
    total_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error: str | None


async def evaluate_tier(
    registry: ModelRegistry,
    clock: Clock,
    tier: str,
    questions: Sequence[Question],
    *,
    request_timeout_ms: float = 300_000.0,
) -> list[Answer]:
    """Ask one tier every question, one at a time, in order."""
    worker = registry.get(tier)
    # One request first, thrown away, so that loading the model is not part of a latency.
    await worker.submit(GenerateRequest("warm-up", tier, "Reply with the letter A.\nAnswer:", 2))
    answers = []
    for question in questions:
        request = GenerateRequest(question.id, tier, build_prompt(question), MAX_TOKENS)
        try:
            result = await run_with_timeout(clock, worker.submit(request), request_timeout_ms)
        except Exception as exc:
            answers.append(
                Answer(
                    id=question.id,
                    subject=question.subject,
                    gold=question.answer,
                    output="",
                    chosen=None,
                    correct=False,
                    ttft_ms=None,
                    total_ms=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        chosen = parse_choice(result.text)
        answers.append(
            Answer(
                id=question.id,
                subject=question.subject,
                gold=question.answer,
                output=result.text,
                chosen=chosen,
                correct=chosen == question.answer,
                ttft_ms=result.time_to_first_token_ms,
                total_ms=result.latency_ms,
                prompt_tokens=result.tokens_in,
                completion_tokens=result.tokens_out,
                cost_usd=result.cost_usd,
                error=None,
            )
        )
    return answers


def summarise(answers: Sequence[Answer]) -> dict[str, Any]:
    """Accuracy with its interval, and what the requests took.

    A request that failed, or a reply that does not open with an option letter, counts as
    a wrong answer: accuracy is correct answers over questions asked.
    """
    total = len(answers)
    correct = sum(1 for a in answers if a.correct)
    low, high = wilson_interval(correct, total)
    served = [a for a in answers if a.error is None]
    latencies = [a.total_ms for a in served if a.total_ms is not None]
    ttfts = [a.ttft_ms for a in served if a.ttft_ms is not None]
    return {
        "questions": total,
        "correct": correct,
        "accuracy": correct / total,
        "ci95": {"low": low, "high": high, "method": "Wilson score"},
        "unparsed": sum(1 for a in served if a.chosen is None),
        "failed": total - len(served),
        "latency_ms": {
            "ttft_p50": percentile(ttfts, 50) if ttfts else None,
            "total_p50": percentile(latencies, 50) if latencies else None,
            "total_p99": percentile(latencies, 99) if latencies else None,
            "total_max": max(latencies) if latencies else None,
        },
        "mean_prompt_tokens": sum(a.prompt_tokens for a in served) / len(served)
        if served
        else None,
        "mean_completion_tokens": (
            sum(a.completion_tokens for a in served) / len(served) if served else None
        ),
        # PLACEHOLDER prices (operator-supplied, not data) applied to the tokens the server
        # reported. No document quotes this.
        "cost_per_1000_requests_usd": (
            1000 * sum(a.cost_usd for a in served) / len(served) if served else None
        ),
    }


async def evaluate(
    config: BackendConfig,
    source: dict[str, Any],
    questions: Sequence[Question],
    *,
    tiers: Sequence[Tier],
    clock: Clock,
    client: httpx.AsyncClient,
    measured_at: str,
    machine: dict[str, Any],
    notes: str = "",
) -> dict[str, Any]:
    """Put every question to every tier and build the results document."""
    sampling = {"temperature": TEMPERATURE, "seed": SAMPLING_SEED}
    registry = build_llamacpp_registry(config, clock, client=client, sampling=sampling)
    document: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "what_this_is": (
            f"Accuracy of each tier's model on {len(questions)} multiple-choice questions, "
            "asked once each on the machine described under 'hardware'. A small sample and a "
            "single run: read the intervals, not just the percentages."
        ),
        "measured_at": measured_at,
        "hardware": machine,
        "endpoint": config.endpoint,
        "notes": notes,
        "questions": {**source, "count": len(questions)},
        "method": {
            "temperature": TEMPERATURE,
            "seed": SAMPLING_SEED,
            "max_tokens": MAX_TOKENS,
            "prompt_template": PROMPT_TEMPLATE,
            "prompt_template_chosen_by": (
                "--pilot: three candidate templates on eight made-up questions, before the "
                "evaluation set was used"
            ),
            "scoring": (
                "exact match on the option letter the reply opens with; a reply that does "
                "not open with A, B, C or D, or a failed request, is wrong"
            ),
            "concurrency": 1,
            "runs": 1,
        },
        "servers": {},
        "tiers": {},
        "answers": {},
        "differences": {},
    }
    by_tier: dict[str, list[Answer]] = {}
    for tier in tiers:
        name = tier.value
        server = await describe_server(client, config.servers[tier])
        answers = await evaluate_tier(registry, clock, name, questions)
        # Asked straight after, while the model is still loaded.
        placement = await describe_placement(client, config.servers[tier])
        limited = hardware_limited_note([placement]) if placement else None
        document["servers"][name] = {**server, "placement": placement}
        document["tiers"][name] = {
            "model_profile": PROFILES[tier].name,
            **summarise(answers),
            "hardware_limited": bool(limited) if placement else None,
            "notes": [limited] if limited else [],
        }
        document["answers"][name] = [dataclasses.asdict(a) for a in answers]
        by_tier[name] = answers
    names = list(by_tier)
    for i, better in enumerate(names):
        for worse in names[:i]:
            # Larger tier minus smaller, in the order the tiers were given.
            document["differences"][f"{better}_minus_{worse}"] = paired_difference(
                [a.correct for a in by_tier[better]], [a.correct for a in by_tier[worse]]
            )
    return document


def summary(document: dict[str, Any]) -> str:
    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}"

    lines = [
        f"{'tier':9} {'model':28} {'correct':>9} {'accuracy':>8} {'95% CI':>15} "
        f"{'unparsed':>8} {'failed':>6} {'p50 ms':>7} {'p99 ms':>7} {'USD/1000':>9}"
    ]
    for tier, t in document["tiers"].items():
        ci = t["ci95"]
        cost = t["cost_per_1000_requests_usd"]
        lines.append(
            f"{tier:9} {document['servers'][tier]['model']:28} "
            f"{t['correct']:4d}/{t['questions']:<4d} {100 * t['accuracy']:7.1f}% "
            f"{100 * ci['low']:6.1f}-{100 * ci['high']:.1f}% {t['unparsed']:8d} {t['failed']:6d} "
            f"{ms(t['latency_ms']['total_p50']):>7} {ms(t['latency_ms']['total_p99']):>7} "
            f"{'-' if cost is None else format(cost, '.4f'):>9}"
        )
        lines.extend(f"  note: {note}" for note in t["notes"])
    for pair, d in document["differences"].items():
        lines.append(
            f"{pair.replace('_', ' ')}: {100 * d['difference']:+.1f} points "
            f"(95% CI {100 * d['low']:+.1f} to {100 * d['high']:+.1f})"
        )
    return "\n".join(lines)


async def _measure(args: argparse.Namespace) -> dict[str, Any]:
    config = load_backend_config(args.backends, kind_override="llamacpp")
    source, questions = load_questions(args.questions)
    now = datetime.datetime.now(datetime.UTC)
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
        return await evaluate(
            config,
            source,
            questions,
            tiers=[Tier(name) for name in args.tiers.split(",")],
            clock=WallClock(),
            client=client,
            measured_at=now.isoformat(timespec="seconds"),
            machine=describe_machine(),
            notes=args.notes,
        )


async def _pilot(args: argparse.Namespace) -> list[dict[str, Any]]:
    config = load_backend_config(args.backends, kind_override="llamacpp")
    sampling = {"temperature": TEMPERATURE, "seed": SAMPLING_SEED}
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
        registry = build_llamacpp_registry(config, WallClock(), client=client, sampling=sampling)
        return await pilot(registry, args.tiers.split(","))


async def _fetch() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await fetch_sample(client, WallClock(), now)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure answer quality on real models.")
    parser.add_argument("--backends", type=Path, help="backends file naming the model servers")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--tiers", default="economy,standard,premium")
    parser.add_argument("--notes", default="", help="free text stored in the output")
    parser.add_argument(
        "--fetch", action="store_true", help="download the question set again and stop"
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="try the candidate templates on made-up questions and stop",
    )
    args = parser.parse_args()
    if args.fetch:
        sample = asyncio.run(_fetch())
        args.questions.parent.mkdir(parents=True, exist_ok=True)
        args.questions.write_text(
            json.dumps(sample, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"wrote {len(sample['items'])} questions to {args.questions} (MMLU, MIT license)")
        return
    if args.pilot:
        rows = asyncio.run(_pilot(args))
        print("MEASURED: template pilot on made-up questions, not the evaluation set.")
        print(pilot_summary(rows))
        return
    document = asyncio.run(_measure(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print("MEASURED on this machine against real model servers. One run, small sample.")
    print(json.dumps(document["hardware"], indent=2))
    print(summary(document))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
