# Contributing

This project is built in small steps, each planned before it is built, tested by breaking its
own rules on purpose, and measured before any number is written down. The full working
agreement is in [CLAUDE.md](CLAUDE.md); this is the short version.

## Setup

```sh
uv sync          # Python 3.11+ and uv
make check       # lint, format check, type check, tests, performance gate
cd frontend && npm ci && npm run check    # the dashboard and the console (Node 20)
```

No `make`? Run the commands from the [Makefile](Makefile) directly.

## Plan first

- **Each step gets its own branch and pull request.** The first commit is the plan: what will
  be built, the decisions it takes that the request did not, and what it will not build. Plans
  live in `docs/*-plan.md`; a step that starts records how it was agreed (for example the
  settings and alerts plan's Part F).
- **Approval before building** is needed for anything that touches authentication, secrets,
  provider keys, a migration that rebuilds a table, or a new network surface. Everything else
  writes its plan as the first commit and continues.
- **Every step ships with tests and a short design note**, `docs/NN-topic.md`: what was decided
  and why ([index](docs/README.md)). A later step that changes an earlier one says so.
- **Pull requests open as drafts** with a description in a fixed order: what changed; decisions
  not in the request, numbered so they can be objected to; mutation checks (a table); timing
  (with the hardware); threat-model additions; what was not built; the checks; the CI result.

## Mutation checks

Every rule about who may do what, or what is refused, has a mutation check:

1. Break the rule in the code (skip the check, grant the role, return early, store the secret).
2. Run the tests that should catch it, and show them failing.
3. Restore the file byte for byte, and show the sha256 before and after match.
4. Run the tests again and show them passing.

A mutation that survives is classified before anything else. If it shows a real rule that no
test can fail, stop and ask. If it sits on dead code or a check a second layer duplicates,
remove the dead code or strengthen the test, and list it in the pull request.

Alongside them:

- **Refusals in authentication** (logins, keys, step-up, codes) get a structural test that every
  case takes the same steps, and the early-return mutation.
- **A missing resource and a forbidden one** get the same response, byte for byte.
- **Any new path that handles a secret** gets a leak scan with a planted control that must be
  found, so the scan is shown to be able to see it.

## Measurement

- **Never write down a number that was not measured.** Every figure in the README, the notes, a
  commit or a pull request names the command and the commit that produced it.
- **Measure on the machine at hand,** record the hardware, and give a range over three runs.
- **Label simulated numbers as simulated**, and never compare simulated and real ones as if they
  measured the same thing. With real models the model server does the batching, so a real-mode
  number is never a result of this repository's scheduler.
- **No invented data in results.** `TEST_PROFILES` are test fixtures; prices and KV capacities
  are placeholders; no batch past the measured 4 in a reported result; a result resting on an
  unstable calibrated parameter says so.
- **The performance gate** (`scripts/perf_gate.py`, budgets in `benchmarks/budgets.json`) fails
  the build on a p99 regression of more than 2%. If a change moves simulated latency on purpose,
  re-baseline with `uv run python scripts/perf_gate.py --update-budgets`, commit the budgets in
  the same change, and paste the printed diff into the pull request. Never re-baseline to make an
  unexplained failure pass.

## Protected outputs

The gate's results, the load generator's output, the report and the tracked benchmark data must
not change unless a change means them to. Their sha256 sums are in
[scripts/protected_outputs.sha256](scripts/protected_outputs.sha256), and CI fails if any differs:

```sh
uv run python scripts/protected_outputs.py                   # regenerate, compare
uv run python scripts/protected_outputs.py --include-images  # also the report figures, this platform
```

The report's figures are not in the cross-platform baseline (the same numbers give different PNG
bytes on Linux and Windows); `--include-images` checks them against
`scripts/protected_outputs.images-<platform>.sha256`, a local check. **A changed sum is a failure
to explain, not a file to refresh.** Refresh only for an intended, understood change: note which
sums changed and why, run `--update`, and commit the new baseline on its own with the reason.

## Before a pull request is done

All of these pass, with the real output in the description:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                                   # with GATEWAY_TEST_POSTGRES_URL and GATEWAY_TEST_VALKEY_URL set
uv run python scripts/check_characters.py      # stage new files first, so it sees them
uv run python scripts/protected_outputs.py --include-images
uv run python scripts/perf_gate.py
uv run python scripts/readme_quickstart.py
cd frontend && npm run check && npm run e2e
```

Then every CI job is green. Postgres and Valkey for the tests come from
`docker-compose.postgres.yml` and `docker-compose.valkey.yml`. Tests that need a real model are
marked `real_models` and skipped unless `GATEWAY_REAL_MODELS=1`; CI never runs them.

## Stop and ask

- a check fails and the fix would change a protected output, a budget or a tolerance;
- a security rule is ambiguous;
- a secret, key or account is needed;
- a test cannot be made to fail under mutation;
- an unexpected file is tracked, or a dependency would be added (pin it, check its license and
  provenance, and record it in [docs/licenses.md](docs/licenses.md) first);
- anything would need a destructive git command.

## The rules that are not negotiable

- **Injectable clock.** No `time.sleep`, `time.time` or `asyncio.sleep` in logic; ruff enforces it.
- **One backend interface;** the simulated backend stays deterministic for a seed.
- **`app.py` is the only wiring point;** logic modules never import FastAPI.
- **Secrets only from the environment;** API keys stored only as an HMAC.
- **No prompt, response or token id stored, logged or measured.**
- **No secret in logs, errors, audit entries, test output, pull requests or commits.**

## Style

Ruff formats the code. mypy runs in strict mode over `src`, `tests`, `scripts` and
`benchmarks`. Full type hints, frozen dataclasses for values, `from __future__ import
annotations`, comments that say why. Tracked code is printable ASCII
(`scripts/check_characters.py`).
