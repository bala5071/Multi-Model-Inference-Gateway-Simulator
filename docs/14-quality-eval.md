# 14 — Answer quality of the three tiers

`benchmarks/quality_eval.py` asks each tier's real model a small fixed set of
multiple-choice questions and records how many it gets right. It is the first thing in this
repository that says what a cheaper plan gives up, not only what it saves.

Code: `benchmarks/quality_eval.py`, `sampling` on `LlamaCppBackend` and
`build_llamacpp_registry`, section 7 of `benchmarks/report.py`.
Data: `benchmarks/quality/` (questions, license, results). Tests: `tests/test_quality_eval.py`.

## Read this first

- **It is small and it was run once.** 100 questions, each asked once per model. The 95%
  interval around each accuracy is 18 to 19 points wide. Nothing was repeated, so there is
  no run-to-run spread to report.
- **Accuracy is measured on real models.** No cost is set beside it: prices were never
  measured (note 01), so no saving is reported. Earlier drafts did, on invented prices; that
  was removed (note 13).
- **It measures one narrow thing:** picking a letter on exam-style questions, with no room
  to reason first. It is not a measure of how good a model's answers to a tenant's own
  requests are.

## The question set and its license

| | |
|---|---|
| Source | MMLU, "Measuring Massive Multitask Language Understanding", Hendrycks et al., 2021 (arXiv:2009.03300) |
| Copy used | `cais/mmlu` on Hugging Face, config `all`, split `test` (14,042 questions over 57 subjects) |
| **License** | **MIT**, Copyright (c) 2020 Dan Hendrycks. Checked on 2026-10-09 in two places: the `LICENSE` file of the authors' repository (`github.com/hendrycks/test`) and the `license: mit` tag of the Hugging Face dataset card |
| What is in this repository | 100 questions in `benchmarks/quality/mmlu_sample.json`, and the license text, as MIT requires, in `benchmarks/quality/LICENSE-MMLU.txt` |
| Selection | `random.Random(7).sample(range(14042), 100)`, sorted, as row numbers of the test split. Fixed before any model was asked anything |
| Fetch again | `uv run python -m benchmarks.quality_eval --fetch` (fails if the split no longer has 14,042 rows) |

The sample has 26, 27, 27 and 20 questions whose right answer is A, B, C and D. It is a
random draw across subjects, not balanced by subject, so it says nothing per subject.

Many MMLU questions are themselves drawn from published exams. The MIT license covers the
dataset as its authors released it; this note makes no claim beyond that.

## Method

| | |
|---|---|
| Path | The gateway's own `LlamaCppBackend` and `PassthroughWorker`, as in real-model mode |
| Sampling | temperature 0, seed 7, at most 8 output tokens |
| Order | One request at a time, the questions in file order, one tier after another. One warm-up request per tier first, thrown away |
| Scoring | **Exact match on the option letter the reply opens with.** `B`, `B.`, `(B)` and `B) newton` are B. A reply that does not open with A, B, C or D, such as "The correct answer is B", is wrong. So is a failed request |
| Interval | Wilson score interval, 95% |
| Between tiers | Difference in accuracy on the same questions, with a 95% Wald interval from the questions the two models disagree on. Approximate at this size |

`LlamaCppBackend` did not send any sampling settings before this step. It now takes
`sampling`, extra request fields sent with every request; without it nothing is sent and
the server's defaults apply, as before.

Temperature 0 makes a run repeatable in principle. It was not checked here: the set was run
once.

### The prompt template

```
The following is a multiple choice question about {subject}.

{question}
A. {a}
B. {b}
C. {c}
D. {d}

Reply with the letter of the correct option only: A, B, C or D.
Answer:
```

It was chosen with a pilot **on eight made-up questions, none from MMLU**, before the
evaluation set was sent to any model:

```sh
uv run python -m benchmarks.quality_eval --backends examples/backends.ollama.yaml --pilot
```

| Template | economy | standard | premium | Replies with no option letter |
|---|---:|---:|---:|---:|
| `instructed` (used) | 7 of 8 | 6 of 8 | 7 of 8 | 0 |
| `mmlu` (the usual MMLU header, no worked examples) | 0 of 8 | 2 of 8 | 0 of 8 | 22 of 24 |
| `question_first` | 6 of 8 | 6 of 8 | 6 of 8 | 0 |

- With the usual MMLU header the chat-tuned models answered in sentences ("The correct
  answer is..."), which exact match scores as wrong. That template would have measured
  whether a model follows a format it was never told about.
- **All three models got "What is 7 + 5?" wrong with every template.** Asked for a letter
  and nothing else, they have no room to work anything out.
  Questions that need a calculation are therefore marked down for every tier.

## Results

Measured on 2026-10-09, one run. Tool as it is in the commit that added this note.

```sh
uv run python -m benchmarks.quality_eval --backends examples/backends.ollama.yaml
```

| | |
|---|---|
| Machine | Intel Core Ultra 7 155H (22 logical cores), 15.4 GB RAM, NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB), Windows 11 (10.0.26200), Python 3.12.10 |
| Server | Ollama 0.12.6, started with `OLLAMA_NUM_PARALLEL=4`, default 4096-token context, `/v1/completions` |
| Models | `llama3.2:1b-instruct-q4_K_M`, `llama3.2:3b`, `llama3.1:8b`; all report Q4_K_M |
| Where they ran | economy and standard 100% on the GPU; **premium 95% GPU, 5% CPU (hardware-limited)**, which affects its latency, not its answers |

| Tier | Model | Correct | Accuracy | 95% interval | Replies with no option letter | p50 / p99 latency of these requests |
|----------|-------|--------:|----:|-------------:|---:|---------------:|
| economy  | Llama 3.2 1B | 33 of 100 | 33% | 24.6% to 42.7% | 17 | 121 / 172 ms |
| standard | Llama 3.2 3B | 52 of 100 | 52% | 42.3% to 61.5% |  0 | 144 / 251 ms |
| premium  | Llama 3.1 8B | 57 of 100 | 57% | 47.2% to 66.3% |  0 | 202 / 459 ms |

Guessing would score about 25%. No request failed.

Differences on the same questions:

| | Difference | 95% interval | |
|---|---:|---:|---|
| standard over economy | +19 points | +9.1 to +28.9 | excludes zero |
| premium over economy  | +24 points | +14.4 to +33.6 | excludes zero |
| premium over standard |  +5 points | -2.5 to +12.5 | **includes zero** |

What can and cannot be said:

- **The 1B model is clearly worse than the other two on this set.**
- **The 3B and 8B models cannot be told apart at this sample size.** Premium scored five
  points higher, on 10 questions it alone got right against 5 that only standard got right.
  A real five-point gap would need a set several times larger to show.
- **Part of the 1B model's deficit is format, not knowledge.** 17 of its replies did not
  open with a letter (14 began "The correct answer is") and count as wrong. Of the 83 that
  did, 33 were right, which is 40%. Exact match was the rule set in advance and it stands;
  the 40% is given so the size of that effect is visible, not as a second score.
- **Every model leans towards option A.** A was the right answer 26 times. Economy chose it
  44 times, standard 44 and premium 54. Economy chose C once, though C was right 27 times.
  The scores are partly a measure of that bias, as the pilot's arithmetic question already
  suggested.
- **The latencies are of a prompt of 131 to 146 tokens, as the servers count them, with a
  one-letter answer.** They are real but describe no workload in the rest of this
  repository.

Every question, reply and score is in `benchmarks/quality/results.json`.

## What a plan gives up

Section 7 of `docs/REPORT.md` sets the accuracy of the best model each plan includes against
premium's, on the same questions:

| Plan | Best model included | Measured accuracy | Given up against premium |
|---|---|---:|---:|
| free       | economy  | 33% | 24 points (14.4 to 33.6) |
| pro        | standard | 52% | 5 points (-2.5 to 12.5) |
| enterprise | premium  | 57% | nothing |

(`uv run python -m benchmarks.report --only quality`; the measurement above.)

- **Free gives up a lot**: about 24 points of accuracy on this set, an interval that
  excludes zero.
- **Pro's loss is not resolved by this measurement**: somewhere between nothing and 12
  points.
- What a plan saves is not reported: prices are placeholders, operator-supplied, not data.
  The accuracy is an estimate from 100 questions of one kind, not a statement about a real
  deployment.

## Limitations

- **One run, 100 questions.** Intervals of 18 to 19 points; a five-point difference between
  two models was not resolved.
- **Multiple choice with a forced one-letter answer.** No reasoning, no long-form answers,
  nothing about tone, safety, tool use or following a tenant's instructions.
- **One template.** A different template moves the numbers, as the pilot showed. The lean
  towards option A is a property of this template with these models.
- **Not comparable with published MMLU scores**, which use five worked examples per subject,
  the whole test split, and usually read the model's probabilities for the four letters.
- **Quantized models (Q4_K_M) through Ollama.** Other builds may score differently.
- **Replies were capped at 8 tokens.** A model that reasons before answering is cut off and
  marked wrong.
- **Not part of CI and not re-measured by `make report`.** The report reads
  `results.json`; changing the question file, the template or the scoring rule means
  measuring again.
- **The simulator still does not model quality.** Routing by `quality` in `docs/09` ranks
  models by tier, not by a measured score.

## Design decisions

- **The set is committed, not downloaded at run time.** The evaluation is then the same
  questions for everyone, and the license text travels with them.
- **The questions are a random draw, not hand-picked**, and the draw is one line of code.
- **Scoring is deliberately strict and was fixed first.** A lenient parser would have raised
  the 1B model's score and made the rule something chosen after seeing results.
- **Unparsed replies and failures count as wrong and are also counted separately**, so a
  reader can see how much of a score is format.
- **The template was chosen on made-up questions**, and the pilot is a command in the tool,
  so the choice can be checked.
- **Hardware and server details are recorded by the calibration tool's functions**,
  including where Ollama loaded each model.
- **The tool reads the real date and uses `WallClock`**, like `calibrate.py`, and is exempt
  from the clock lint rule for the same reason.
