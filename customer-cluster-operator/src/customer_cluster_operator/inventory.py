"""Kubespray inventory rendering and authenticated Git publication."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationError


def inventory_document(resources: dict[str, Any]) -> dict[str, Any]:
    jump_ip = resources["jumphost"]["floating_ip"]
    ssh_common = f"-o StrictHostKeyChecking=accept-new -o ProxyJump=root@{jump_ip}"
    hosts = {
        node["name"]: {
            "ansible_host": node["ip"],
            "ip": node["ip"],
            "access_ip": node["ip"],
            "ansible_user": "root",
            "ansible_ssh_common_args": ssh_common,
        }
        for node in resources["controllers"] + resources["workers"]
    }
    controllers = {node["name"]: {} for node in resources["controllers"]}
    nodes = {node["name"]: {} for node in resources["controllers"] + resources["workers"]}
    return {
        "all": {
            "hosts": hosts,
            "children": {
                "kube_control_plane": {"hosts": controllers},
                "kube_node": {"hosts": nodes},
                "etcd": {"hosts": controllers},
                "k8s_cluster": {
                    "children": {
                        "kube_control_plane": {},
                        "kube_node": {},
                    }
                },
                "calico_rr": {"hosts": {}},
            },
        }
    }


def render_inventory(resources: dict[str, Any]) -> str:
    return (
        "---\n"
        "# GENERATED FILE: customer-cluster-operator. Manual edits will be overwritten.\n"
        + yaml.safe_dump(inventory_document(resources), sort_keys=False)
    )


def _git_env(repo_url: str, username: str, token: str) -> dict[str, str]:
    if "\n" in username or "\r" in username:
        raise ValidationError("Git username contains an invalid newline")
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{repo_url}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _run_git(
    args: list[str], cwd: Path | None, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
    )


def publish_inventory(
    git_config: dict[str, Any], slug: str, inventory: str, token: str, retries: int = 3
) -> tuple[str, str]:
    """Commit and push from a fresh clone, retrying non-fast-forward races."""
    if not token:
        raise ValidationError("Git token is empty")
    branch = git_config["branch"]
    if branch.startswith("-") or "\n" in branch or "\r" in branch:
        raise ValidationError("Git branch is invalid")
    relative = f"clusters/{slug}/generated/ansible/hosts.yml"
    env = _git_env(git_config["repoUrl"], git_config["username"], token)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with tempfile.TemporaryDirectory(prefix="cluster-inventory-") as temp:
                repo = Path(temp) / "repo"
                _run_git(
                    [
                        "clone",
                        "--quiet",
                        "--single-branch",
                        "--branch",
                        branch,
                        git_config["repoUrl"],
                        str(repo),
                    ],
                    None,
                    env,
                )
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(inventory)
                _run_git(["add", "--", relative], repo, env)
                changed = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=repo,
                    env=env,
                    check=False,
                    timeout=30,
                ).returncode
                if changed == 0:
                    commit = _run_git(["rev-parse", "HEAD"], repo, env).stdout.strip()
                    return relative, commit
                _run_git(
                    [
                        "-c",
                        "user.name=customer-cluster-operator",
                        "-c",
                        "user.email=customer-cluster-operator@sunet.se",
                        "commit",
                        "--quiet",
                        "-m",
                        f"Generate Kubespray inventory for {slug}",
                    ],
                    repo,
                    env,
                )
                _run_git(["push", "--quiet", "origin", f"HEAD:{branch}"], repo, env)
                commit = _run_git(["rev-parse", "HEAD"], repo, env).stdout.strip()
                return relative, commit
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"failed to publish generated inventory after {retries} attempts"
    ) from last_error
