"""The character gate: no zero-width, bidirectional-control or other non-ASCII characters in
tracked code, except the dashboard's few allowed typographic ones. See
scripts/check_characters.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_characters import ROOT, check, check_text, is_checked, tracked_files

ZERO_WIDTH_SPACE = chr(0x200B)
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
ELLIPSIS = chr(0x2026)


def test_the_tracked_files_are_clean() -> None:
    assert check(ROOT, tracked_files(ROOT)) == []


@pytest.mark.parametrize(
    ("character", "name"),
    [
        (ZERO_WIDTH_SPACE, "ZERO WIDTH SPACE"),
        (chr(0x200D), "ZERO WIDTH JOINER"),
        (chr(0xFEFF), "ZERO WIDTH NO-BREAK SPACE"),
        (RIGHT_TO_LEFT_OVERRIDE, "RIGHT-TO-LEFT OVERRIDE"),
        (chr(0x2066), "LEFT-TO-RIGHT ISOLATE"),
        (chr(0x0430), "CYRILLIC SMALL LETTER A"),
        (chr(0x00E9), "LATIN SMALL LETTER E WITH ACUTE"),
        (chr(0x00A0), "NO-BREAK SPACE"),
        (chr(0x07), "CONTROL CHARACTER"),
    ],
)
def test_a_planted_character_is_found_where_it_is(character: str, name: str) -> None:
    text = f"ok = 1\nvalue = 'a{character}b'\n"
    [finding] = check_text("src/gateway/example.py", text)
    assert (finding.line, finding.column) == (2, 11)
    assert str(finding).endswith(name)


def test_the_dashboard_may_show_its_typography_but_never_invisible_characters() -> None:
    assert check_text("frontend/src/tabs/Example.tsx", f"<p>Loading{ELLIPSIS}</p>\n") == []
    assert check_text("src/gateway/example.py", f"# Loading{ELLIPSIS}\n") != []
    for invisible in (ZERO_WIDTH_SPACE, RIGHT_TO_LEFT_OVERRIDE):
        assert check_text("frontend/src/tabs/Example.tsx", f"<p>a{invisible}b</p>\n") != []


def test_which_files_are_checked() -> None:
    for path in (
        "src/gateway/app.py",
        "frontend/src/App.tsx",
        ".github/workflows/ci.yml",
        "scripts/run_local_models.sh",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
    ):
        assert is_checked(path), path
    for path in (
        "docs/27-users-and-sessions.md",
        "benchmarks/quality/mmlu_sample.json",
        "src/gateway/data/common-passwords.txt",
        "docs/img/demo.gif",
    ):
        assert not is_checked(path), path


def test_a_file_that_is_not_utf8_is_a_finding(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"x = '\xff'\n")
    [finding] = check(tmp_path, ["bad.py"])
    assert finding.path == "bad.py"
