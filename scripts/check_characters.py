"""Fail on characters that do not belong in code: a CI gate, like perf_gate.py.

Every tracked source, test, configuration, workflow and shell file must be printable ASCII
(plus tab and newlines). A zero-width or bidirectional-control character can make code read
differently from what it does, and a look-alike letter can hide in a name, so they are
refused everywhere. Write such a character as an escape (``"\\u200b"``) instead.

The allowlist is short and explicit: the dashboard's sources may use a few typographic
characters in the text they show (an ellipsis, a middle dot, a multiplication sign, an en
dash, curly quotes, a check mark). Even there, any control, format (zero-width, bidirectional)
or other invisible character is refused. Markdown docs and data files (JSON, the bundled
blocklist) are not checked: they hold prose and public data, not code.

    uv run python scripts/check_characters.py

Exit 0 when clean; otherwise each finding as ``path:line:column U+XXXX NAME``, and exit 1.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKED_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".css",
        ".html",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".sh",
        ".sql",
    }
)
CHECKED_NAMES = frozenset(
    {"Dockerfile", "Makefile", ".gitattributes", ".gitignore", ".dockerignore", ".editorconfig"}
)
ASCII_CONTROLS_ALLOWED = frozenset("\t\n\r")

# Path pattern -> the only non-ASCII characters it may contain.
ALLOWLIST: dict[str, frozenset[str]] = {
    "frontend/src/*": frozenset(
        "\u2026\u00b7\u00d7\u2013\u201c\u201d\u2713"  # ellipsis, middle dot, times, en dash,
    ),  # curly quotes, check mark: text the dashboard shows
    "frontend/e2e/*": frozenset("\u00d7\u201c\u201d"),
}


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    column: int
    character: str

    def __str__(self) -> str:
        fallback = (
            "CONTROL CHARACTER" if unicodedata.category(self.character) == "Cc" else "UNNAMED"
        )
        name = unicodedata.name(self.character, fallback)
        return f"{self.path}:{self.line}:{self.column} U+{ord(self.character):04X} {name}"


def is_checked(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in CHECKED_NAMES or any(name.endswith(s) for s in CHECKED_SUFFIXES)


def allowed_for(path: str) -> frozenset[str]:
    return frozenset().union(
        *(chars for pattern, chars in ALLOWLIST.items() if fnmatch.fnmatch(path, pattern))
    )


def never_allowed(character: str) -> bool:
    """Controls, format characters (zero-width, bidirectional) and other invisible ones."""
    return unicodedata.category(character)[0] in "CZ" and character != " "


def check_text(path: str, text: str) -> list[Finding]:
    allowed = allowed_for(path)
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for column, character in enumerate(line, start=1):
            if " " <= character <= "~" or character in ASCII_CONTROLS_ALLOWED:
                continue
            if character in allowed and not never_allowed(character):
                continue
            findings.append(Finding(path, number, column, character))
    return findings


def tracked_files(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [p for p in listed.split("\0") if p]


def check(root: Path, paths: Iterable[str]) -> list[Finding]:
    findings = []
    for path in sorted(paths):
        if not is_checked(path):
            continue
        data = (root / path).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            findings.append(Finding(path, 0, exc.start, "\ufffd"))
            continue
        findings.extend(check_text(path, text))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = check(ROOT, tracked_files(ROOT))
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"{len(findings)} character(s) not allowed; write them as escapes (docs: the script)."
        )
        return 1
    print("No disallowed characters in tracked source, test, workflow and shell files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
