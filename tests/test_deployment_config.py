"""The compose file, the Dockerfile and the example env file: no secret in any of them, the
database on a volume, and the port on this machine only. Docker itself is not run by the tests;
CI's docker job builds the image and starts the compose demo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from gateway.apikeys import PEPPER_ENV

REPO = Path(__file__).resolve().parent.parent


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.yml").read_text("utf-8"))
    return data


def _active_lines(path: Path) -> list[str]:
    """Lines that are not comments or blank."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_compose_publishes_on_this_machine_only_and_keeps_the_database_on_a_volume() -> None:
    gateway = _compose()["services"]["gateway"]
    assert gateway["ports"] == ["127.0.0.1:8000:8000"]
    assert "gateway-data:/data" in gateway["volumes"]
    assert "gateway-data" in _compose()["volumes"]
    assert gateway["env_file"] == [{"path": "gateway.env", "required": False}]


def test_no_secret_is_in_the_compose_file_or_the_image() -> None:
    environment = _compose()["services"]["gateway"].get("environment", {})
    assert PEPPER_ENV not in environment
    for path in (REPO / "docker-compose.yml", REPO / "Dockerfile"):
        active = "\n".join(_active_lines(path))
        assert PEPPER_ENV not in active, path.name
        assert "gwk_" not in active, path.name
    # The Dockerfile sets no environment variable that could hold a secret.
    env_lines = [line for line in _active_lines(REPO / "Dockerfile") if line.startswith("ENV")]
    assert all("PEPPER" not in line and "KEY" not in line for line in env_lines)


def test_the_example_env_file_has_placeholders_only() -> None:
    values = dict(line.split("=", 1) for line in _active_lines(REPO / "gateway.env.example"))
    assert set(values) == {
        PEPPER_ENV,
        "GATEWAY_VAULT_KEY",
        "GATEWAY_DATABASE",
        "GATEWAY_USAGE_RETENTION_DAYS",
        "GATEWAY_ADMIN_USERNAME",
        "GATEWAY_ADMIN_PASSWORD",
        "GATEWAY_AUDIT_RETENTION_DAYS",
    }
    # Every secret is a placeholder, never a value.
    assert re.fullmatch(r"<[^>]+>", values[PEPPER_ENV])
    assert re.fullmatch(r"<[^>]+>", values["GATEWAY_VAULT_KEY"])
    assert re.fullmatch(r"<[^>]+>", values["GATEWAY_ADMIN_PASSWORD"])
    assert values["GATEWAY_DATABASE"] == "/data/gateway.db"  # on the volume


def test_secrets_and_databases_stay_out_of_the_build_context() -> None:
    ignored = set(_active_lines(REPO / ".dockerignore"))
    assert {"gateway.env", "*.db", "*.db-*", "*.sqlite3"} <= ignored


def test_the_image_starts_through_the_launcher_with_the_network_bind_opt_in() -> None:
    active = _active_lines(REPO / "Dockerfile")
    assert "ENV GATEWAY_ALLOW_NETWORK_BIND=1" in active
    [command] = [line for line in active if line.startswith("CMD")]
    assert '"-m", "gateway.serve"' in command
    assert "uvicorn" not in command


def test_compose_counts_only_the_docker_bridge_as_local() -> None:
    environment = _compose()["services"]["gateway"]["environment"]
    assert environment["GATEWAY_LOCAL_NETWORKS"] == "172.16.0.0/12"


def test_every_launch_command_uses_the_launcher() -> None:
    for name in ("README.md", "Makefile", "frontend/playwright.config.ts"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "uvicorn gateway.app:create_default_app" not in text, name
        assert "gateway.serve" in text, name


def test_the_readme_makes_its_admin_password_at_run_time_and_keeps_it_out_of_git() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assignments = re.findall(r"GATEWAY_ADMIN_PASSWORD=[^\n]*", readme)
    # Each makes a random value when it runs (the quickstart's into gateway.env, "Try the chat
    # endpoint" into a variable); no password is written down.
    assert assignments == [
        "GATEWAY_ADMIN_PASSWORD=' + secrets.token_urlsafe(24))\" >> gateway.env",
        'GATEWAY_ADMIN_PASSWORD=$(uv run python -c "import secrets; '
        'print(secrets.token_urlsafe(24))")',
    ]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "gateway.env"], cwd=REPO, check=False
    )
    assert ignored.returncode == 0
