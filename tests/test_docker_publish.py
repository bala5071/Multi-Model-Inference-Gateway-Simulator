"""No example publishes the gateway's port on every interface.

The gateway has no authentication on its monitoring and load-control endpoints
(docs/platform-plan.md, section 9), so every Docker example must publish on 127.0.0.1 only.
`-p 8000:8000` (or a compose `ports:` entry of the same form) publishes on 0.0.0.0.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", "dashboard", ".mypy_cache", ".ruff_cache"}
SUFFIXES = {".md", ".yml", ".yaml", ".sh", ".toml"}
NAMES = {"Dockerfile", "Makefile"}

# `-p 8000:8000`, `--publish=8000:8000`, `-p "8000:8000"`: a host port with no host address.
UNSAFE_RUN = re.compile(r"(?:\s-p|--publish)[\s=]+[\"']?\d+:\d+")
# A compose port entry with no host address: `- "8000:8000"` or `- 8000:8000`.
UNSAFE_COMPOSE = re.compile(r"^\s*-\s*[\"']?\d+:\d+(?:/\w+)?[\"']?\s*(?:#.*)?$")


def _files() -> list[Path]:
    found = []
    for path in REPO.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        if path.is_file() and (path.suffix in SUFFIXES or path.name in NAMES):
            found.append(path)
    return sorted(found)


def _unsafe_lines(path: Path) -> list[str]:
    compose = path.name.startswith(("docker-compose", "compose")) and path.suffix in (
        ".yml",
        ".yaml",
    )
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if UNSAFE_RUN.search(line) or (compose and UNSAFE_COMPOSE.match(line)):
            hits.append(f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()}")
    return hits


def test_no_docker_example_publishes_on_every_interface() -> None:
    offenders = [hit for path in _files() for hit in _unsafe_lines(path)]
    assert not offenders, "Publish on 127.0.0.1 only (-p 127.0.0.1:8000:8000):\n" + "\n".join(
        offenders
    )


def test_the_check_catches_the_unsafe_forms() -> None:
    for line in ("docker run --rm -p 8000:8000 img", "docker run --publish=8000:8000 img"):
        assert UNSAFE_RUN.search(line), line
    for line in ("docker run --rm -p 127.0.0.1:8000:8000 img", "uvicorn app --port 8000"):
        assert not UNSAFE_RUN.search(line), line
    assert UNSAFE_COMPOSE.match('      - "8000:8000"')
    assert not UNSAFE_COMPOSE.match('      - "127.0.0.1:8000:8000"')
