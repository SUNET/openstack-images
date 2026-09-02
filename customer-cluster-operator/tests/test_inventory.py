import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.inventory import (
    _git_env,
    inventory_document,
    publish_inventory,
    render_inventory,
)


@pytest.fixture
def resources():
    return {
        "jumphost": {"name": "jump", "floating_ip": "192.0.2.10"},
        "controllers": [
            {"name": "controller-01", "ip": "10.0.0.10"},
            {"name": "controller-02", "ip": "10.0.0.11"},
            {"name": "controller-03", "ip": "10.0.0.12"},
        ],
        "workers": [{"name": "worker-01", "ip": "10.0.0.20"}],
    }


def test_inventory_has_standard_groups_and_proxyjump(resources):
    document = inventory_document(resources)
    all_group = document["all"]
    assert set(all_group["children"]) == {
        "kube_control_plane",
        "kube_node",
        "etcd",
        "k8s_cluster",
        "calico_rr",
    }
    host = all_group["hosts"]["controller-01"]
    assert host["ansible_user"] == "root"
    assert host["ansible_host"] == host["access_ip"] == "10.0.0.10"
    assert "ProxyJump=root@192.0.2.10" in host["ansible_ssh_common_args"]


def test_rendered_inventory_has_warning_and_no_credentials(resources):
    rendered = render_inventory(resources)
    assert "GENERATED FILE" in rendered
    assert "token" not in rendered.lower()
    assert yaml.safe_load(rendered)["all"]["hosts"]


def test_git_auth_is_process_scoped(monkeypatch):
    monkeypatch.setenv("UNCHANGED", "yes")
    repo_url = "https://git.example/repo.git"
    env = _git_env(repo_url, "bot", "super-secret")
    assert env["GIT_CONFIG_KEY_0"] == f"http.{repo_url}.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "super-secret" not in env["GIT_CONFIG_VALUE_0"]
    assert "GIT_CONFIG_KEY_0" not in __import__("os").environ


def test_publish_uses_env_not_url_or_command(monkeypatch):
    calls = []

    def fake_git(args, cwd, env):
        calls.append((args, env))
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return Mock(returncode=0, stdout="a" * 40 + "\n")

    monkeypatch.setattr("customer_cluster_operator.inventory._run_git", fake_git)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: Mock(returncode=1),
    )
    config = {
        "repoUrl": "https://git.example/repo.git",
        "branch": "main",
        "username": "bot",
    }
    path, commit = publish_inventory(config, "example", "inventory", "secret")
    assert path.endswith("hosts.yml")
    assert commit == "a" * 40
    assert all("secret" not in " ".join(args) for args, _ in calls)
    assert all("secret" not in config["repoUrl"] for _ in calls)


def test_publish_rejects_empty_token():
    with pytest.raises(ValidationError, match="empty"):
        publish_inventory({}, "example", "inventory", "")


def test_unchanged_inventory_returns_head_without_commit_or_push(monkeypatch):
    calls = []

    def fake_git(args, cwd, env):
        calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return Mock(returncode=0, stdout="c" * 40 + "\n")

    monkeypatch.setattr("customer_cluster_operator.inventory._run_git", fake_git)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Mock(returncode=0))
    config = {
        "repoUrl": "https://git.example/repo.git",
        "branch": "main",
        "username": "bot",
    }
    path, commit = publish_inventory(config, "example", "inventory", "secret")
    assert path.endswith("hosts.yml")
    assert commit == "c" * 40
    assert not any("commit" in args or "push" in args for args in calls)
