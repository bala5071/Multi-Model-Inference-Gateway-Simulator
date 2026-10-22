# Common tasks. Everything runs through uv, so the only prerequisite is uv itself.
# On Windows without make, run the command on the right-hand side directly.

.PHONY: install lint format typecheck test gate check bench report demo serve serve-sim
.PHONY: calibrate dashboard dashboard-check dashboard-dev dashboard-e2e pictures

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
	uv run python -m gateway.serve --port 8000

# The gateway in simulated live mode: no models, a seeded load replayed on simulated time
# at 5 times real time. Everything it shows is simulated. See
# docs/17-playground-load-replay.md.
serve-sim:
	GATEWAY_SIMULATED_LIVE=1 GATEWAY_SIM_SPEED=5 uv run python -m gateway.serve --port 8000

# Builds the monitoring dashboard into src/gateway/dashboard, which `make serve` then
# serves at /dashboard. Needs Node 20. See docs/16-dashboard.md.
dashboard:
	cd frontend && npm ci && npm run build

# Lint, formatting, types and component tests of the dashboard.
dashboard-check:
	cd frontend && npm run check

# Browser smoke tests: builds the dashboard and runs it against the gateway in simulated
# live mode. Needs a browser: `npx playwright install chromium` in frontend/, or set
# PLAYWRIGHT_CHANNEL=chrome. See docs/18-demo.md.
dashboard-e2e:
	cd frontend && npm run e2e

# Retakes the pictures in docs/img. Same browser needs as dashboard-e2e.
pictures:
	uv run python scripts/capture_demo.py

# The dashboard with hot reload on http://localhost:5173/dashboard/, proxying the API to
# a gateway on port 8000 (`make serve` in another terminal).
dashboard-dev:
	cd frontend && npm run dev

# Measures real models; needs model servers running. See docs/13-calibration.md.
# Example: make calibrate BACKENDS=examples/backends.ollama.yaml
BACKENDS ?= src/gateway/backends.yaml
calibrate:
	uv run python -m benchmarks.calibrate --backends $(BACKENDS) --out benchmarks/calibration
