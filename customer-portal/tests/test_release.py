"""Release metadata and image build contracts; no database or image daemon required."""

from __future__ import annotations

import ast
import re
import runpy
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

PORTAL_ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.1.24"
KUSTOMIZE_VERSION = "v5.8.0"
DEPLOYMENT = PORTAL_ROOT.parents[2] / "k8s/platform-manifests/customer-portal/base/deployment.yaml"


@pytest.fixture
def docker_instructions() -> list[tuple[str, str]]:
    source = (PORTAL_ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = []
    for line in source.replace("\\\n", " ").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            instruction, value = line.split(maxsplit=1)
            instructions.append((instruction.upper(), value))
    return instructions


def test_release_metadata_matches_jenkins_image_tag() -> None:
    project = tomllib.loads((PORTAL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    jenkins = yaml.safe_load((PORTAL_ROOT.parent / ".jenkins.yaml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == RELEASE_VERSION
    assert jenkins["environment_variables"]["PORTAL_VERSION"] == RELEASE_VERSION
    jobs = [job for job in jenkins["extra_jobs"] if job["name"] == "customer-portal"]
    assert len(jobs) == 1
    assert jobs[0]["docker_name"] == "platform/customer-portal"
    assert jobs[0]["docker_context_dir"] == "customer-portal"
    assert "${PORTAL_VERSION}" in jobs[0]["docker_tags"]


def test_app_metadata_uses_release_version_module() -> None:
    version_module = PORTAL_ROOT / "app/_version.py"
    if not version_module.is_file():
        pytest.skip("The separately maintained app/_version.py is not present")

    assert runpy.run_path(str(version_module))["VERSION"] == RELEASE_VERSION
    main = ast.parse((PORTAL_ROOT / "app/main.py").read_text(encoding="utf-8"))
    imported_versions = {
        alias.asname or alias.name
        for node in ast.walk(main)
        if isinstance(node, ast.ImportFrom) and node.module in {"app._version", "_version"}
        for alias in node.names
        if alias.name == "VERSION"
    }
    api_versions = [
        keyword.value
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
        for keyword in node.keywords
        if keyword.arg == "version"
    ]
    assert len(api_versions) == 1
    assert isinstance(api_versions[0], ast.Name)
    assert api_versions[0].id in imported_versions


def test_deployment_image_matches_release_when_checkout_is_available() -> None:
    if not DEPLOYMENT.is_file():
        pytest.skip("The sibling k8s/platform-manifests checkout is not present")

    deployment = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    images = [container["image"] for container in containers if container["name"] == "portal"]
    assert images == [f"docker.sunet.se/platform/customer-portal:{RELEASE_VERSION}"]


def test_browser_tooling_is_a_development_dependency() -> None:
    project = tomllib.loads((PORTAL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = project["project"]["optional-dependencies"]["dev"]

    assert any(dependency.startswith("playwright>=") for dependency in dev)
    assert not any(
        dependency.startswith("playwright") for dependency in project["project"]["dependencies"]
    )
    assert any(
        marker.startswith("browser:")
        for marker in project["tool"]["pytest"]["ini_options"]["markers"]
    )


def test_image_bases_are_pinned_and_follow_target_platform(
    docker_instructions: list[tuple[str, str]],
) -> None:
    stages = [
        shlex.split(value) for instruction, value in docker_instructions if instruction == "FROM"
    ]

    assert len(stages) == 2
    assert re.fullmatch(r"golang:\d+\.\d+\.\d+-trixie@sha256:[0-9a-f]{64}", stages[0][0])
    assert stages[0][1:] == ["AS", "kustomize-builder"]
    assert re.fullmatch(r"python:\d+\.\d+\.\d+-slim-trixie", stages[1][0])
    assert not any(token.startswith("--platform=") for stage in stages for token in stage)


def test_kustomize_build_pins_and_verifies_modules(
    docker_instructions: list[tuple[str, str]],
) -> None:
    runtime_start = max(
        index
        for index, (instruction, _) in enumerate(docker_instructions)
        if instruction == "FROM"
    )
    builder = docker_instructions[:runtime_start]
    environment = dict(
        token.split("=", 1)
        for instruction, value in builder
        if instruction == "ENV"
        for token in shlex.split(value)
    )
    assert environment["CGO_ENABLED"] == "0"
    assert environment["GOTOOLCHAIN"] == "local"
    assert environment["GOPROXY"] == "https://proxy.golang.org"
    assert environment["GOSUMDB"] == "sum.golang.org"
    assert not any(environment.get(name) for name in ("GONOSUMDB", "GOINSECURE", "GOPRIVATE"))

    install_commands = [
        shlex.split(value)
        for instruction, value in builder
        if instruction == "RUN" and "go install" in value
    ]
    assert len(install_commands) == 1
    assert install_commands[0][:2] == ["go", "install"]
    assert "-trimpath" in install_commands[0]
    assert install_commands[0][-1] == f"sigs.k8s.io/kustomize/kustomize/v5@{KUSTOMIZE_VERSION}"


def test_dockerfile_does_not_require_buildkit(
    docker_instructions: list[tuple[str, str]],
) -> None:
    """The Jenkins Docker plugin uses the legacy builder, including multi-stage COPY."""
    for instruction, value in docker_instructions:
        if instruction in {"COPY", "ADD"}:
            flags = [token for token in shlex.split(value) if token.startswith("--")]
            assert all(flag.startswith(("--from=", "--chown=")) for flag in flags)
        elif instruction == "RUN":
            assert not value.startswith(("--", "<<"))


def test_runtime_checks_standalone_kustomize_as_nonroot_user(
    docker_instructions: list[tuple[str, str]],
) -> None:
    runtime_start = max(
        index
        for index, (instruction, _) in enumerate(docker_instructions)
        if instruction == "FROM"
    )
    runtime = docker_instructions[runtime_start:]
    copies = [shlex.split(value) for instruction, value in runtime if instruction == "COPY"]
    assert [
        "--from=kustomize-builder",
        "/go/bin/kustomize",
        "/usr/local/bin/kustomize",
    ] in copies
    assert [value for instruction, value in runtime if instruction == "USER"] == ["portal"]
    nonroot_start = runtime.index(("USER", "portal"))
    copy_step = runtime.index((
        "COPY", "--from=kustomize-builder /go/bin/kustomize /usr/local/bin/kustomize"
    ))
    permission_step = runtime.index(("RUN", "chmod 0555 /usr/local/bin/kustomize"))
    assert copy_step < permission_step < nonroot_start
    checks = [value for instruction, value in runtime[nonroot_start:] if instruction == "RUN"]
    assert any(
        'test "$(command -v kustomize)" = "/usr/local/bin/kustomize"' in check
        and f'test "$(kustomize version)" = "{KUSTOMIZE_VERSION}"' in check
        for check in checks
    )
