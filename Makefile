# Common tasks. Everything runs through uv, so the only prerequisite is uv itself.
# On Windows without make, run the command on the right-hand side directly.

.PHONY: install lint format typecheck test gate check bench report demo serve calibrate

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

# Fails if any model's simulated p99 is over its budget. See docs/08-ci-gate.md.
gate:
	uv run python scripts/perf_gate.py

# Everything CI runs.
check: lint typecheck test gate

# Every simulated benchmark, printed to the terminal. All deterministic.
bench:
	uv run python -m benchmarks.baseline_limits
	uv run python -m benchmarks.compare_batching
	uv run python -m benchmarks.loadgen
	uv run python -m benchmarks.fairness
	uv run python -m benchmarks.overload
	uv run python -m benchmarks.chunked_prefill
	uv run python -m benchmarks.metrics_example

# Regenerates docs/REPORT.md and docs/report/*.png. Sections 1 to 5 are simulated and
# reproduce exactly. Section 6 reads benchmarks/calibration/ and does not re-measure.
report:
	uv run python -m benchmarks.report

# A short tour in simulator mode: plans, a 403, a cache hit, streaming, usage. No server.
demo:
	uv run python scripts/demo.py

# The gateway on http://127.0.0.1:8000 in simulator mode. API docs at /docs.
serve:
	uv run uvicorn gateway.app:create_default_app --factory --port 8000

# Measures real models; needs model servers running. See docs/13-calibration.md.
# Example: make calibrate BACKENDS=examples/backends.ollama.yaml
BACKENDS ?= src/gateway/backends.yaml
calibrate:
	uv run python -m benchmarks.calibrate --backends $(BACKENDS) --out benchmarks/calibration
