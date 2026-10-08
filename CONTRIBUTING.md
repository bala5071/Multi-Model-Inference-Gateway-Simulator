# Contributing

Small project, short rules.

## Setup

```sh
uv sync          # Python 3.11+ and uv are the only prerequisites
make check       # lint, type check, tests, performance gate
```

No `make`? Run the commands from the [Makefile](Makefile) directly.

## Before you open a pull request

1. `make check` passes. CI runs the same four steps.
2. New behaviour comes with tests. Timing tests use `SimClock`, never real sleeps.
3. A change of any size to how the gateway behaves gets a short note in `docs/`, or an
   update to the note it belongs to.
4. If you changed simulated latency on purpose, re-baseline the gate
   (`uv run python scripts/perf_gate.py --update-budgets`), commit
   `benchmarks/budgets.json`, and paste the printed diff into the pull request.
5. If results changed, regenerate the report with `make report`. Do not edit
   `docs/REPORT.md` by hand.

## The rules that are not negotiable

They are spelled out in [CLAUDE.md](CLAUDE.md). In short:

- **Injectable clock.** No `time.sleep`, `time.time` or `asyncio.sleep` in logic. Ruff
  enforces it; do not silence it.
- **One backend interface.** `SimulatedBackend` stays deterministic for a seed.
- **Only `app.py` imports FastAPI** and wires things together.
- **Never write down a number that was not measured.** Every figure names the command that
  produced it. Simulated numbers are labelled simulated. Real-model numbers come with the
  hardware they were measured on.
- **Do not present a real-model number as a result of this repo's scheduler.** With real
  models the model server does the batching.

## Tests that need a real model

They are marked `real_models` and skipped unless `GATEWAY_REAL_MODELS=1`. CI never runs
them. See [docs/12-real-models.md](docs/12-real-models.md).

## Style

Ruff formats the code (`make format`). mypy runs in strict mode. Match the code around
you: full type hints, frozen dataclasses for values, comments that say why.
