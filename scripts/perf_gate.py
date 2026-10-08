"""CI performance gate. Exits non-zero if any model's p99 total latency is over budget.

    uv run python scripts/perf_gate.py                    # check against the budgets
    uv run python scripts/perf_gate.py --update-budgets   # re-baseline (not in CI)
    uv run python scripts/perf_gate.py --simulate-regression premium=2   # see it fail

The logic is in benchmarks/gate.py. See docs/08-ci-gate.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The benchmarks package lives at the repo root, which is not on the path when this file
# is run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.gate import main

if __name__ == "__main__":
    sys.exit(main())
