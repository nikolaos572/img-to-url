"""requirements.txt pins the exact package versions the Docker image installs."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent


def _locked() -> dict[str, Requirement]:
    locked = {}
    for line in (REPO_ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            requirement = Requirement(line)
            locked[canonicalize_name(requirement.name)] = requirement
    return locked


def test_every_package_is_pinned_to_one_version():
    locked = _locked()
    assert locked
    for name, requirement in locked.items():
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", (
            f"{name} must be pinned with ==, got {requirement}"
        )


def test_runtime_dependencies_are_locked_within_their_ranges():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    locked = _locked()
    for dependency in project["dependencies"]:
        wanted = Requirement(dependency)
        name = canonicalize_name(wanted.name)
        assert name in locked, f"{wanted.name} is missing from requirements.txt"
        version = next(iter(locked[name].specifier)).version
        assert wanted.specifier.contains(version), (
            f"requirements.txt pins {wanted.name}=={version}, outside "
            f"pyproject.toml's range {wanted.specifier}"
        )
