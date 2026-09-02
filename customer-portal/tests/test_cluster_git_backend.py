"""Tests for rendering cluster desired-state manifests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import git
import pytest
import yaml

from app.cluster_git_backend import ClusterDeletionBlocked, ClusterGitBackend
from app.config import Settings
from app.git_backend import GitBackend, ManagedProjectMutationError
from app.git_url import git_auth_environment
from app.routers.clusters import _sync_managed_project_admins


@pytest.fixture
def backend(tmp_path: Path, monkeypatch) -> ClusterGitBackend:
    settings = Settings(
        project_git_repo_url="/unused",
        project_git_username="project-bot",
        project_git_token="project-token",
        cluster_git_repo_url="/unused",
        cluster_git_work_dir=str(tmp_path),
        cluster_dns_zone="k8s-test.sunetvdc.se",
    )
    instance = ClusterGitBackend(settings)
    instance.clusters_dir.mkdir()
    monkeypatch.setattr(instance, "_pull", lambda: None)
    monkeypatch.setattr(instance, "_commit_and_push", lambda _message: None)
    return instance


@pytest.fixture
def project_backend(tmp_path: Path, monkeypatch) -> GitBackend:
    settings = Settings(
        project_git_repo_url="/unused",
        project_git_work_dir=str(tmp_path),
        default_domain="customer-sso",
        cluster_provisioner_user="provisioner",
        cluster_provisioner_user_domain="service-users",
    )
    instance = GitBackend(settings)
    instance.projects_dir.mkdir()
    monkeypatch.setattr(instance, "_pull", lambda: None)
    monkeypatch.setattr(instance, "_commit_and_push", lambda _message: None)
    return instance


def test_managed_project_keeps_customer_and_provisioner_bindings(
    project_backend: GitBackend,
) -> None:
    resource_name = project_backend.write_project(
        contract_number="CO-001",
        project_name="cluster.umu.se",
        description="Managed cluster",
        users=["admin@umu.se"],
        managed=True,
    )

    path = project_backend.projects_dir / f"{resource_name}.yaml"
    assert "  roleBindings:\n    - role: reader" in path.read_text()
    document = yaml.safe_load(path.read_text())
    assert document["spec"]["roleBindings"] == [
        {
            "role": "reader",
            "users": ["admin@umu.se"],
            "userDomain": "customer-sso",
        },
        {
            "role": "member",
            "users": ["provisioner"],
            "userDomain": "service-users",
        },
    ]
    assert project_backend.get_project(resource_name)["users"] == ["admin@umu.se"]

    project_backend.update_project(resource_name, users=["new-admin@umu.se"])

    document = yaml.safe_load(path.read_text())
    assert document["spec"]["roleBindings"] == [
        {
            "role": "reader",
            "users": ["new-admin@umu.se"],
            "userDomain": "customer-sso",
        },
        {
            "role": "member",
            "users": ["provisioner"],
            "userDomain": "service-users",
        },
    ]


def test_self_service_project_binding_is_unchanged(
    project_backend: GitBackend,
) -> None:
    resource_name = project_backend.write_project(
        contract_number="CO-001",
        project_name="research.umu.se",
        description="Self service",
        users=["member@umu.se"],
    )

    path = project_backend.projects_dir / f"{resource_name}.yaml"
    document = yaml.safe_load(path.read_text())
    assert document["spec"]["roleBindings"] == [
        {
            "role": "member",
            "users": ["member@umu.se"],
            "userDomain": "customer-sso",
        }
    ]
    assert "managed" not in document["spec"]


def test_managed_project_cannot_be_moved(project_backend: GitBackend) -> None:
    resource_name = project_backend.write_project(
        contract_number="CO-001",
        project_name="cluster.umu.se",
        description="Managed cluster",
        users=[],
        managed=True,
    )

    with pytest.raises(ManagedProjectMutationError, match="read-only"):
        project_backend.move_project(resource_name, "CO-002")

    assert project_backend.get_project(resource_name)["contract_number"] == "CO-001"


def test_managed_project_prevents_partial_contract_rename(
    project_backend: GitBackend,
) -> None:
    unmanaged = project_backend.write_project(
        contract_number="CO-001",
        project_name="research.umu.se",
        description="Self service",
        users=[],
    )
    managed = project_backend.write_project(
        contract_number="CO-001",
        project_name="cluster.umu.se",
        description="Managed cluster",
        users=[],
        managed=True,
    )

    with pytest.raises(ManagedProjectMutationError, match="decommissioning"):
        project_backend.rename_contract("CO-001", "CO-002")

    assert project_backend.get_project(unmanaged)["contract_number"] == "CO-001"
    assert project_backend.get_project(managed)["contract_number"] == "CO-001"


async def test_customer_admin_sync_preserves_provisioner_binding() -> None:
    settings = Settings(
        project_git_repo_url="/unused",
        default_domain="customer-sso",
        cluster_provisioner_user="provisioner",
        cluster_provisioner_user_domain="service-users",
    )
    cluster = SimpleNamespace(id=42, management_project_resource_name="cluster-umu-se")
    session = SimpleNamespace(execute=AsyncMock())
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        "z-admin@umu.se",
        "a-admin@umu.se",
    ]
    session.execute.return_value = result
    git_backend = MagicMock()

    await _sync_managed_project_admins(cluster, session, git_backend, settings)

    git_backend.update_project.assert_called_once_with(
        resource_name="cluster-umu-se",
        role_bindings=[
            {
                "role": "reader",
                "users": ["a-admin@umu.se", "z-admin@umu.se"],
                "userDomain": "customer-sso",
            },
            {
                "role": "member",
                "users": ["provisioner"],
                "userDomain": "service-users",
            },
        ],
    )


def test_write_cluster_renders_environment_specific_manifest(
    backend: ClusterGitBackend,
) -> None:
    path = backend.write_cluster(
        slug="umu-one",
        display_name="UMU cluster one",
        contract_number="CO-001",
        customer_domain="umu.se",
        worker_groups=2,
        project_name="umu-one.umu.se",
        project_resource_name="umu-one-umu-se",
    )

    assert path == "clusters/umu-one/cluster.yaml"
    document = yaml.safe_load((backend.work_dir / path).read_text())
    assert document["metadata"]["name"] == "umu-one"
    assert document["spec"]["suspend"] is False
    assert document["spec"]["deletionPolicy"] == "Retain"
    assert document["spec"]["profileRef"] == {"name": "standard-v1"}
    assert document["spec"]["workerGroups"] == 2
    assert document["spec"]["openstack"] == {
        "projectName": "umu-one.umu.se",
        "projectResourceName": "umu-one-umu-se",
    }
    assert document["spec"]["dns"] == {
        "zone": "k8s-test.sunetvdc.se",
        "apiHostname": "api.umu-one.k8s-test.sunetvdc.se",
        "argocdHostname": "argocd.umu-one.k8s-test.sunetvdc.se",
    }
    assert document["spec"]["openbao"] == {
        "mount": "kubernetes/umu-one",
        "secretRoot": "kv/customer-clusters/umu-one",
    }


def test_unsupported_profile_name_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_PROFILE_NAME", "gpu-v2")

    with pytest.raises(RuntimeError, match="must be 'standard-v1'"):
        Settings(project_git_repo_url="/unused")


def test_write_cluster_updates_root_kustomization_deterministically(
    backend: ClusterGitBackend,
) -> None:
    generated = backend.clusters_dir / "aaa" / "inventory.yaml"
    generated.parent.mkdir()
    generated.write_text("generated: true\n")

    for slug in ("z-last", "m-middle"):
        backend.write_cluster(
            slug=slug,
            display_name=slug,
            contract_number="CO-001",
            customer_domain="umu.se",
            worker_groups=1,
            project_name=f"{slug}.umu.se",
            project_resource_name=f"{slug}-umu-se",
        )

    document = yaml.safe_load((backend.work_dir / "kustomization.yaml").read_text())
    assert (
        "resources:\n  - clusters/m-middle/cluster.yaml"
        in (backend.work_dir / "kustomization.yaml").read_text()
    )
    assert document == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [
            "clusters/m-middle/cluster.yaml",
            "clusters/z-last/cluster.yaml",
        ],
    }


def test_write_cluster_rejects_existing_manifest(
    backend: ClusterGitBackend,
) -> None:
    values = {
        "slug": "umu-one",
        "display_name": "UMU cluster one",
        "contract_number": "CO-001",
        "customer_domain": "umu.se",
        "worker_groups": 1,
        "project_name": "umu-one.umu.se",
        "project_resource_name": "umu-one-umu-se",
    }
    backend.write_cluster(**values)

    assert backend.write_cluster(**values) == "clusters/umu-one/cluster.yaml"

    values["worker_groups"] = 2
    with pytest.raises(ValueError, match="already exists"):
        backend.write_cluster(**values)


def test_matching_manifest_repairs_kustomization_only_when_needed(
    backend: ClusterGitBackend,
    monkeypatch,
) -> None:
    values = {
        "slug": "umu-one",
        "display_name": "UMU cluster one",
        "contract_number": "CO-001",
        "customer_domain": "umu.se",
        "worker_groups": 1,
        "project_name": "umu-one.umu.se",
        "project_resource_name": "umu-one-umu-se",
    }
    backend.write_cluster(**values)
    commits = []
    monkeypatch.setattr(backend, "_commit_and_push", commits.append)
    (backend.work_dir / "kustomization.yaml").write_text("resources: []\n")

    assert backend.write_cluster(**values) == "clusters/umu-one/cluster.yaml"
    assert commits == ["Repair cluster kustomization for umu-one"]
    assert backend.write_cluster(**values) == "clusters/umu-one/cluster.yaml"
    assert commits == ["Repair cluster kustomization for umu-one"]


@pytest.mark.parametrize("suspend", [False, True, None])
def test_delete_cluster_refuses_every_published_manifest_without_changes(
    backend: ClusterGitBackend,
    monkeypatch,
    suspend: bool | None,
) -> None:
    path = backend.write_cluster(
        slug="umu-one",
        display_name="UMU cluster one",
        contract_number="CO-001",
        customer_domain="umu.se",
        worker_groups=1,
        project_name="umu-one.umu.se",
        project_resource_name="umu-one-umu-se",
    )
    manifest_path = backend.work_dir / path
    manifest = yaml.safe_load(manifest_path.read_text())
    if suspend is None:
        del manifest["spec"]["suspend"]
    else:
        manifest["spec"]["suspend"] = suspend
    manifest_path.write_text(yaml.dump(manifest, sort_keys=False))
    kustomization_path = backend.work_dir / "kustomization.yaml"
    before_manifest = manifest_path.read_bytes()
    before_kustomization = kustomization_path.read_bytes()
    commits = []
    monkeypatch.setattr(backend, "_commit_and_push", commits.append)

    with pytest.raises(ClusterDeletionBlocked, match="portal deletion is disabled"):
        backend.delete_cluster("umu-one")

    assert manifest_path.read_bytes() == before_manifest
    assert kustomization_path.read_bytes() == before_kustomization
    assert commits == []


def test_delete_cluster_refuses_and_preserves_generated_directory(
    backend: ClusterGitBackend,
) -> None:
    path = backend.write_cluster(
        slug="umu-one",
        display_name="UMU cluster one",
        contract_number="CO-001",
        customer_domain="umu.se",
        worker_groups=1,
        project_name="umu-one.umu.se",
        project_resource_name="umu-one-umu-se",
    )
    generated = backend.clusters_dir / "umu-one" / "generated"
    generated.mkdir()
    (generated / "inventory.yaml").write_text("generated: true\n")
    manifest_path = backend.work_dir / path

    with pytest.raises(ClusterDeletionBlocked, match="portal deletion is disabled"):
        backend.delete_cluster("umu-one")

    assert manifest_path.exists()
    assert (generated / "inventory.yaml").exists()


def test_rejected_push_raises_git_error(backend: ClusterGitBackend) -> None:
    result = MagicMock()
    result.flags = git.remote.PushInfo.REJECTED
    result.summary = "non-fast-forward"
    repo = MagicMock()
    repo.remotes.origin.push.return_value = [result]
    backend.repo = repo

    with pytest.raises(git.GitCommandError, match="non-fast-forward"):
        ClusterGitBackend._commit_and_push(backend, "test", max_retries=1)


def test_failed_write_restores_disposable_clone(
    backend: ClusterGitBackend,
    monkeypatch,
) -> None:
    restored = []
    monkeypatch.setattr(
        backend,
        "_commit_and_push",
        lambda _message: (_ for _ in ()).throw(RuntimeError("push failed")),
    )
    monkeypatch.setattr(backend, "_restore_origin", lambda: restored.append(True))

    with pytest.raises(RuntimeError, match="push failed"):
        backend.write_cluster(
            slug="umu-one",
            display_name="UMU cluster one",
            contract_number="CO-001",
            customer_domain="umu.se",
            worker_groups=1,
            project_name="umu-one.umu.se",
            project_resource_name="umu-one-umu-se",
        )

    assert restored == [True]


def test_git_auth_environment_uses_independent_credentials() -> None:
    environment = git_auth_environment(
        "https://clusters.example.org/SUNET/customer-clusters-test.git",
        "cluster bot",
        "cluster/token",
    )
    assert environment["GIT_CONFIG_KEY_0"] == ("http.https://clusters.example.org/.extraHeader")
    assert environment["GIT_CONFIG_VALUE_0"] == (
        "Authorization: Basic Y2x1c3RlciBib3Q6Y2x1c3Rlci90b2tlbg=="
    )
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_git_auth_environment_rejects_embedded_credentials() -> None:
    with pytest.raises(RuntimeError, match="must not contain credentials"):
        git_auth_environment(
            "https://old-token@platform.sunet.se/SUNET/customer-projects.git",
            "project-bot",
            "project-token",
        )


@pytest.mark.parametrize(
    ("backend_type", "url_field", "work_dir_field", "repo_url"),
    [
        (
            GitBackend,
            "project_git_repo_url",
            "project_git_work_dir",
            "https://git.example.org/projects.git",
        ),
        (
            ClusterGitBackend,
            "cluster_git_repo_url",
            "cluster_git_work_dir",
            "https://git.example.org/clusters.git",
        ),
    ],
)
def test_existing_checkout_origin_is_replaced(
    tmp_path: Path,
    monkeypatch,
    backend_type,
    url_field: str,
    work_dir_field: str,
    repo_url: str,
) -> None:
    (tmp_path / ".git").mkdir()
    repo = MagicMock()
    monkeypatch.setattr(git, "Repo", MagicMock(return_value=repo))
    settings_values = {
        "project_git_repo_url": "https://git.example.org/projects.git",
        "project_git_username": "project-bot",
        "project_git_token": "project-token",
        "cluster_git_repo_url": "https://git.example.org/clusters.git",
        "cluster_git_username": "cluster-bot",
        "cluster_git_token": "cluster-token",
        url_field: repo_url,
        work_dir_field: str(tmp_path),
    }
    settings = Settings(**settings_values)

    backend_type(settings).init()

    repo.remotes.origin.set_url.assert_called_once_with(repo_url)
