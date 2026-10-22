"""Run the README's quickstart exactly as written, and check what it printed.

    uv run python scripts/readme_quickstart.py
    uv run python scripts/readme_quickstart.py --port 8123   # if 8000 is taken locally

The shell blocks of the README's Quickstart that follow a ``<!-- quickstart -->`` line are
run in order, as one bash script (``set -euo pipefail``), from the repository root. They
create a throwaway pepper in ``gateway.env``, create the demo applications, issue a key,
start the server, send a request, restart the server and read the usage back. This script
then checks that the request was served and that the usage survived the restart. CI runs it
on every push, so the README's commands cannot stop working unnoticed.

It isolates what the README would otherwise touch: the database goes to a temporary data
folder (``XDG_DATA_HOME`` / ``LOCALAPPDATA``), any ``GATEWAY_*`` variable of the caller is
removed so the README has to set what it needs, an existing ``gateway.env`` is never
overwritten (the script refuses to start), and the one it creates is deleted afterwards.
The key and the pepper are never printed by this script.

It refuses to start if something already answers on the port: the README's health check
would otherwise talk to that server. ``--port`` replaces the README's port 8000 in the
commands, for a machine where 8000 is taken; CI runs them unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ENV_FILE = ROOT / "gateway.env"
MARKER = "<!-- quickstart -->"
README_PORT = 8000
# Stop whatever the README left running, however the script ends.
PRELUDE = "set -euo pipefail\ntrap 'kill $(jobs -p) 2>/dev/null || true' EXIT\n"


def quickstart_blocks(readme: str) -> list[str]:
    """The ``sh`` blocks that directly follow a quickstart marker, in order."""
    pattern = re.compile(re.escape(MARKER) + r"\s*\n```sh\n(.*?)\n```", re.DOTALL)
    return pattern.findall(readme)


def json_objects(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object printed, in order (curl prints no newline after one)."""
    decoder = json.JSONDecoder()
    found, index = [], 0
    while (start := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end
    return found


def check(printed: list[dict[str, Any]]) -> list[str]:
    """What is wrong with the quickstart's output; empty if nothing."""
    problems = []
    answers = [o for o in printed if "usage" in o and "model" in o and "text" in o]
    usages = [o for o in printed if "application_id" in o and "total" in o]
    if len(answers) != 1:
        problems.append(f"expected one generate response, found {len(answers)}")
    elif answers[0]["model"] != "premium" or answers[0]["usage"]["completion_tokens"] < 1:
        problems.append(f"unexpected generate response: {answers[0]}")
    if not usages:
        problems.append("expected the usage response after the restart, found none")
    elif usages[-1]["application_id"] != "acme" or usages[-1]["total"]["requests"] != 1:
        problems.append(f"usage did not survive the restart: {usages[-1]}")
    return problems


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the README quickstart and check it.")
    parser.add_argument("--port", type=int, default=README_PORT)
    args = parser.parse_args(argv)
    blocks = quickstart_blocks(README.read_text(encoding="utf-8"))
    if args.port != README_PORT:
        blocks = [
            b.replace(f"127.0.0.1:{README_PORT}", f"127.0.0.1:{args.port}").replace(
                f"--port {README_PORT}", f"--port {args.port}"
            )
            for b in blocks
        ]
    if _port_in_use(args.port):
        print(f"something already answers on 127.0.0.1:{args.port}; stop it or use --port")
        return 1
    if len(blocks) < 5:
        print(f"found {len(blocks)} quickstart blocks in README.md; expected at least 5")
        return 1
    if ENV_FILE.exists():
        print(f"{ENV_FILE.name} exists; refusing to overwrite it. Move it away and run again.")
        return 1
    with tempfile.TemporaryDirectory() as data:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
        env.update(XDG_DATA_HOME=data, LOCALAPPDATA=data)
        script = PRELUDE + "\n".join(blocks) + "\n"
        try:
            done = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        finally:
            ENV_FILE.unlink(missing_ok=True)
    print(done.stdout)
    if done.returncode != 0:
        print(done.stderr[-4000:])
        print(f"the README quickstart failed (exit {done.returncode})")
        return 1
    problems = check(json_objects(done.stdout))
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    print(f"The README quickstart ran: {len(blocks)} blocks, request served, usage persisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
