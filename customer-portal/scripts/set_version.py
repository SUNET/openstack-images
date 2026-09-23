"""Set the customer portal package and image-tag version together."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

PORTAL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PORTAL_ROOT.parent
PYPROJECT_PATH = PORTAL_ROOT / "pyproject.toml"
JENKINS_PATH = REPOSITORY_ROOT / ".jenkins.yaml"

RELEASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
PROJECT_VERSION = re.compile(r'(?m)^(version\s*=\s*")([^"\n]+)(".*)$')
JENKINS_VERSION = re.compile(r'(?m)^(\s*PORTAL_VERSION:\s*")([^"\n]+)(".*)$')


def _match_once(pattern: re.Pattern[str], content: str, label: str) -> re.Match[str]:
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def _replace_version(pattern: re.Pattern[str], content: str, version: str, label: str) -> str:
    match = _match_once(pattern, content, label)
    return (
        content[: match.start()]
        + match.group(1)
        + version
        + match.group(3)
        + content[match.end() :]
    )


def _write_atomic(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def set_release_version(
    version: str,
    *,
    pyproject_path: Path = PYPROJECT_PATH,
    jenkins_path: Path = JENKINS_PATH,
) -> None:
    """Update both release-version declarations after validating current state."""
    if RELEASE_VERSION.fullmatch(version) is None:
        raise ValueError("version must have the form MAJOR.MINOR.PATCH")

    project_content = pyproject_path.read_text(encoding="utf-8")
    jenkins_content = jenkins_path.read_text(encoding="utf-8")
    project_match = _match_once(PROJECT_VERSION, project_content, "project version")
    jenkins_match = _match_once(JENKINS_VERSION, jenkins_content, "Jenkins portal version")

    if project_match.group(2) != jenkins_match.group(2):
        raise ValueError(
            "current project and Jenkins portal versions are out of sync: "
            f"{project_match.group(2)} != {jenkins_match.group(2)}"
        )

    updated_project = _replace_version(
        PROJECT_VERSION, project_content, version, "project version"
    )
    updated_jenkins = _replace_version(
        JENKINS_VERSION, jenkins_content, version, "Jenkins portal version"
    )

    # Prepare and validate both results before replacing either source file.
    _match_once(PROJECT_VERSION, updated_project, "updated project version")
    _match_once(JENKINS_VERSION, updated_jenkins, "updated Jenkins portal version")
    _write_atomic(pyproject_path, updated_project)
    _write_atomic(jenkins_path, updated_jenkins)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version in MAJOR.MINOR.PATCH form")
    arguments = parser.parse_args()
    set_release_version(arguments.version)
    print(f"Customer portal release version set to {arguments.version}")


if __name__ == "__main__":
    main()
