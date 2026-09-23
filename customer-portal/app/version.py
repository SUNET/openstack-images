"""Resolve the application version from its canonical package metadata."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

DISTRIBUTION_NAME = "customer-portal-api"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


def get_version(
    *,
    distribution: Callable[[str], str] = distribution_version,
    pyproject_path: Path = PYPROJECT_PATH,
) -> str:
    """Return installed metadata, or project metadata for a source checkout."""
    try:
        return distribution(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        with pyproject_path.open("rb") as pyproject_file:
            project = tomllib.load(pyproject_file)["project"]
        if project["name"] != DISTRIBUTION_NAME:
            raise ValueError(
                f"unexpected distribution name {project['name']!r} in {pyproject_path}"
            )
        return project["version"]


VERSION = get_version()
