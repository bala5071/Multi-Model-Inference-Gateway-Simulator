# 00 — Scaffolding

Step 0 sets up the repo layout, tooling and CI. It contains no gateway logic.

## Decisions

- **uv with a committed `uv.lock`.** CI installs with `uv sync --locked`, so local and CI
  environments resolve to the same versions. Hatchling is the build backend.
- **src layout.** Tests run against the installed package, not the working directory, which
  catches packaging mistakes early.
- **Python floor 3.11, CI on 3.11 and 3.12.** The floor is only real if something tests it.
- **The Clock rule is enforced by ruff.** `flake8-tidy-imports` banned-api rejects
  `time.sleep`, `time.time`, `time.monotonic`, `time.perf_counter`, `asyncio.sleep` and
  `datetime.now`/`utcnow`. Only `src/gateway/clock.py` is exempt. A rule that lives only in
  prose gets broken by accident.
- **mypy strict over `src` and `tests`.** Cheaper to start strict than to tighten later.
- **pytest `asyncio_mode = "auto"`, strict markers and config.** Async tests need no
  decorator; typos in markers or config fail instead of being ignored.
- **One smoke test.** It checks that the package installs and its version matches the
  metadata. It also keeps pytest from exiting with "no tests collected".
- **No p99 gate in CI yet.** It is added with the benchmark step, when there is something to
  measure.

## Not in this step

Clock, backend interface, `SimulatedBackend`, batcher, prefix cache, metrics, FastAPI app,
benchmark harness.
