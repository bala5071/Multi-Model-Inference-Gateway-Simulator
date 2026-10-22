"""The helpers of scripts/protected_outputs.py, and the baseline file it checks against.

The full check regenerates the gate, the load generator and the report, which takes too
long for the test suite; it is run by hand after every platform step (docs/platform-plan.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import protected_outputs as po  # noqa: E402


def test_text_is_hashed_with_crlf_read_as_lf_and_binaries_as_they_are() -> None:
    assert po.sha256(b'{"a": 1}\r\n{"b": 2}\r\n') == po.sha256(b'{"a": 1}\n{"b": 2}\n')
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert po.normalise(png) == png
    assert po.sha256(png) != po.sha256(png.replace(b"\r\n", b"\n"))


def test_the_report_is_compared_without_its_commit_line() -> None:
    a = b"# Results report\n\n" + po.COMMIT_LINE + b" from commit `abc1234`.\nx\n"
    b = a.replace(b"abc1234", b"def5678 with uncommitted changes")
    assert po.without_commit_line(a) == po.without_commit_line(b) == b"# Results report\n\nx\n"


def test_sums_round_trip_and_every_kind_of_difference_is_reported() -> None:
    sums = {"gate.json": "a" * 64, "docs/REPORT.md (without its commit line)": "b" * 64}
    assert po.parse_sums(po.format_sums(sums)) == sums
    assert po.differences(sums, dict(sums)) == []

    found = po.differences(sums, {"gate.json": "c" * 64, "new.json": "d" * 64})
    assert [line.split()[0] for line in found] == ["missing", "changed", "new"]


def test_the_committed_baseline_lists_full_sums_and_relative_paths() -> None:
    text = po.BASELINE.read_text(encoding="utf-8")
    sums = po.parse_sums(text)
    assert len(sums) == 27
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in sums.values())
    # Relative paths only: nothing that names a machine, a drive or a home folder.
    assert not any(re.match(r"/|[A-Za-z]:|~", name) or "\\" in name for name in sums)
    assert "\r" not in text
