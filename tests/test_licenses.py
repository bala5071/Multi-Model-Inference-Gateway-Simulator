"""Every runtime dependency has an allowed license, recorded in docs/licenses.md.

The runtime dependencies are found from the installed metadata of the `gateway` package and
everything it requires (with environment markers evaluated, extras left out), so a new
dependency, or a new dependency of a dependency, fails this test until it is recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent
LICENSES_DOC = REPO / "docs" / "licenses.md"
PINNED = ("argon2-cffi", "cryptography", "pyjwt")


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_dependencies() -> dict[str, metadata.Distribution]:
    """The `gateway` package's runtime requirements, transitively, by normalised name."""
    environment = {key: str(value) for key, value in default_environment().items()}
    environment["extra"] = ""
    found: dict[str, metadata.Distribution] = {}
    todo = ["gateway"]
    while todo:
        name = _normal(todo.pop())
        if name in found:
            continue
        dist = metadata.distribution(name)
        found[name] = dist
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate(environment):
                todo.append(requirement.name)
    del found["gateway"]
    return found


def declared_license(dist: metadata.Distribution) -> str:
    """The SPDX expression a package declares (PEP 639), or the one its classifiers imply."""
    fields = dist.metadata.json
    expression = fields.get("license_expression")
    if isinstance(expression, str) and expression:
        return expression
    from_classifiers = {
        "MIT License": "MIT",
        "BSD License": "BSD-3-Clause",
        "Apache Software License": "Apache-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "Python Software Foundation License": "PSF-2.0",
        "ISC License (ISCL)": "ISC",
    }
    classifiers = fields.get("classifier") or []
    for classifier in classifiers if isinstance(classifiers, list) else [classifiers]:
        if classifier.startswith("License ::"):
            return from_classifiers.get(classifier.split(" :: ")[-1], classifier)
    license_field = fields.get("license")
    return license_field if isinstance(license_field, str) and license_field else "UNKNOWN"


def _doc() -> str:
    return LICENSES_DOC.read_text(encoding="utf-8")


def allowed_licenses() -> set[str]:
    match = re.search(r"^Allowed: (.+)$", _doc(), re.MULTILINE)
    assert match, "docs/licenses.md has no 'Allowed:' line"
    return {item.strip() for item in match.group(1).split(",")}


def documented_runtime_table() -> dict[str, str]:
    section = _doc().split("## Every runtime dependency", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| ([a-z0-9-]+) \| ([^|]+) \|$", section, re.MULTILINE)
    return {name: license.strip() for name, license in rows}


def is_allowed(expression: str, allowed: set[str]) -> bool:
    """``A OR B`` is allowed if either is; ``A AND B`` only if both are."""
    return any(
        all(part.strip(" ()") in allowed for part in alternative.split(" AND "))
        for alternative in expression.split(" OR ")
    )


def test_every_runtime_dependency_has_an_allowed_license() -> None:
    allowed = allowed_licenses()
    bad = {
        name: declared_license(dist)
        for name, dist in runtime_dependencies().items()
        if not is_allowed(declared_license(dist), allowed)
    }
    assert bad == {}


def test_every_runtime_dependency_is_in_the_docs_with_its_license() -> None:
    documented = documented_runtime_table()
    installed = {name: declared_license(dist) for name, dist in runtime_dependencies().items()}
    missing = sorted(set(installed) - set(documented))
    assert missing == [], f"add to docs/licenses.md: {missing}"
    wrong = {n: (documented[n], lic) for n, lic in installed.items() if documented[n] != lic}
    assert wrong == {}


def test_the_new_dependencies_are_pinned_exactly_and_documented_at_that_version() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pins = {
        _normal(Requirement(raw).name): str(Requirement(raw).specifier)
        for raw in project["project"]["dependencies"]
    }
    for name in PINNED:
        version = metadata.version(name)
        assert pins[name] == f"=={version}", name
        assert re.search(rf"^\| {name} \| {re.escape(version)} \|", _doc(), re.MULTILINE), name


def test_the_blocklist_is_the_recorded_file_with_its_license() -> None:
    data = REPO / "src" / "gateway" / "data"
    digest = hashlib.sha256((data / "common-passwords.txt").read_bytes()).hexdigest()
    assert f"`{digest}`" in _doc()
    license_text = (data / "LICENSE-SecLists.txt").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert "Daniel Miessler" in license_text


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("Apache-2.0 OR BSD-3-Clause", True),
        ("GPL-3.0-only OR MIT", True),
        ("GPL-3.0-only", False),
        ("MIT AND GPL-3.0-only", False),
        ("UNKNOWN", False),
    ],
)
def test_license_expressions(expression: str, allowed: bool) -> None:
    assert is_allowed(expression, allowed_licenses()) is allowed


def test_the_openai_wire_fixture_is_the_recorded_extract() -> None:
    folder = Path(__file__).parent / "fixtures" / "openai_wire"
    fixture = json.loads((folder / "chat_completions.json").read_text(encoding="utf-8"))
    doc = _doc()
    assert fixture["commit"] in doc
    assert fixture["sha256"] in doc
    assert "MIT License" in (folder / "LICENSE-openai-openapi.txt").read_text(encoding="utf-8")
    assert set(fixture["examples"]) == {"Default", "Streaming", "Functions"}
