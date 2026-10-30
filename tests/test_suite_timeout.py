"""The suite-wide limit per test (tests/conftest.py): no test can hang the run. Each case runs
a test that never finishes, in its own pytest process with a limit of one second."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HANGS = Path(__file__).with_name("hang_cases.py")


def _run(test: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{HANGS}::{test}",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "gateway_test_timeout_s=1",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).parents[1],
    )


def test_an_async_test_past_the_limit_is_cancelled_and_fails() -> None:
    done = _run("test_waits_forever_on_the_event_loop")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "over the suite's limit of 1 s per test (gateway_test_timeout_s)" in done.stdout
    assert "1 failed" in done.stdout


def test_a_test_cancelling_cannot_reach_ends_the_run_at_twice_the_limit() -> None:
    done = _run("test_waits_forever_in_a_blocking_call")
    assert done.returncode != 0
    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        "hang_cases.py::test_waits_forever_in_a_blocking_call: still running after 2 s, twice "
        "the limit per test (gateway_test_timeout_s); ending the run"
    ) in done.stderr, done.stderr
    # Where it waited: the traceback of the test's thread.
    assert re.search(r"line \d+ in test_waits_forever_in_a_blocking_call", done.stderr), done.stderr
