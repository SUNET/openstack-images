"""Real local Git workflows for preview, concurrency, adoption and confirmation.

Every transport is mapped to a disposable bare repository. No test can fall
through to a network URL. Kustomize is the real executable, not a build mock.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import customer_gitops, gitops_git, gitops_validate
from app.config import Settings
from app.customer_gitops import (
    CustomerGitOpsError,
    prepare_preview,
    publish_preview,
    render_tree,
    validate_tree,
)
from app.gitops_git import GitRepository, Transport

REPO_URL = "https://forgejo.example.test/customer/clusters.git"
BASES_URL = "https://forgejo.example.test/public/cluster-bases.git"
TOKEN = "writer-secret-fixture-91c21"
USERNAME = "customer-writer"
INGRESS = "clusters/acme-one/addons/argocd-ingress"
APPS = "clusters/acme-one/argocd-apps"


def git(repo: Path, *args: str, data: str | None = None, check: bool = True) -> str:
    env = gitops_git.isolated_environment()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.test",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        input=data,
        text=True,
        capture_output=True,
        check=check,
    )
    return result.stdout.strip()


def commit(
    repo: Path,
    files: dict[str, str | tuple[str, str]],
    *,
    remove: tuple[str, ...] = (),
    branch: str = "main",
) -> str:
    parent = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    git(repo, "read-tree", parent) if parent else git(repo, "read-tree", "--empty")
    for path in remove:
        git(repo, "update-index", "--index-info", data=f"0 {'0' * 40}\t{path}\n")
    raw_tree = None
    for path, value in files.items():
        mode, content = value if isinstance(value, tuple) else ("100644", value)
        oid = (
            content
            if mode == "160000"
            else git(repo, "hash-object", "-w", "--stdin", data=content)
        )
        if path == ".gitmodules" and mode == "120000":
            # New Git refuses to put this in an index. Construct the hostile
            # remote tree directly so its fetch-time fsck is exercised instead.
            assert len(files) == 1 and not parent
            raw_tree = git(repo, "mktree", data=f"120000 blob {oid}\t.gitmodules\n")
            continue
        git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")
    tree = raw_tree or git(repo, "write-tree")
    args = ["commit-tree", tree]
    if parent:
        args.extend(["-p", parent])
    revision = git(repo, *args, data="Fixture commit\n")
    git(repo, "update-ref", f"refs/heads/{branch}", revision)
    return revision


def _yaml(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=False)


def base_files() -> dict[str, str]:
    """A small offline base exercising the actual reviewed protection contract."""
    namespace = "envoy-gateway-system"
    labels = {"app.kubernetes.io/name": "gateway-helm", "app.kubernetes.io/instance": "eg"}
    docs: list[dict[str, Any]] = []
    for kind in (
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Job",
        "ClusterRole",
        "ClusterRoleBinding",
    ):
        cluster_scoped = kind.startswith("Cluster")
        metadata: dict[str, Any] = {
            "name": "eg-gateway-helm-certgen" + (f":{namespace}" if cluster_scoped else ""),
            "annotations": {"helm.sh/hook": "pre-install, pre-upgrade"},
        }
        if not cluster_scoped:
            metadata["namespace"] = namespace
        docs.append({"apiVersion": "v1", "kind": kind, "metadata": metadata})
    docs.extend(
        [
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "MutatingWebhookConfiguration",
                "metadata": {
                    "name": f"envoy-gateway-topology-injector.{namespace}",
                    "annotations": {
                        "helm.sh/hook": "pre-install, pre-upgrade",
                    },
                },
                "webhooks": [
                    {
                        "name": "topology.webhook.gateway.envoyproxy.io",
                        "clientConfig": {
                            "service": {
                                "name": "envoy-gateway",
                                "namespace": namespace,
                                "path": "/inject-pod-topology",
                                "port": 9443,
                            },
                        },
                        "rules": [{"resources": ["pods/binding"]}],
                    }
                ],
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "envoy-gateway", "namespace": namespace},
                "spec": {
                    "replicas": 2,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "topologySpreadConstraints": [
                                {
                                    "maxSkew": 1,
                                    "topologyKey": "kubernetes.io/hostname",
                                    "whenUnsatisfiable": "ScheduleAnyway",
                                    "labelSelector": {"matchLabels": labels},
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "policy/v1",
                "kind": "PodDisruptionBudget",
                "metadata": {"name": "envoy-gateway", "namespace": namespace},
                "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}},
            },
        ]
    )
    schema: dict[str, Any] = {"type": "array", "items": {"type": "object"}}
    for field in reversed(
        ("spec", "provider", "kubernetes", "envoyDeployment", "pod", "topologySpreadConstraints")
    ):
        schema = {"type": "object", "properties": {field: schema}}
    docs.append(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "envoyproxies.gateway.envoyproxy.io"},
            "spec": {
                "versions": [
                    {"name": "v1alpha1", "served": True, "schema": {"openAPIV3Schema": schema}}
                ]
            },
        }
    )
    kustomization = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["resources.yaml"],
        "patches": [
            {
                "target": {
                    "name": (
                        r"^(eg-gateway-helm-certgen(:envoy-gateway-system)?|"
                        r"envoy-gateway-topology-injector\.envoy-gateway-system)$"
                    )
                },
                "patch": _yaml(
                    [
                        {
                            "op": "add",
                            "path": "/metadata/annotations/argocd.argoproj.io~1sync-options",
                            "value": "Prune=false",
                        },
                        {
                            "op": "add",
                            "path": "/metadata/annotations/argocd.argoproj.io~1compare-options",
                            "value": "IgnoreExtraneous",
                        },
                    ]
                ),
            }
        ],
    }
    files = {
        "envoy-gateway/kustomization.yaml": _yaml(kustomization),
        "envoy-gateway/resources.yaml": yaml.safe_dump_all(docs),
    }
    for name in ("argocd", "cert-manager", "portal-access"):
        files[f"{name}/kustomization.yaml"] = _yaml(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["namespace.yaml"],
            }
        )
        files[f"{name}/namespace.yaml"] = _yaml(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": name},
            }
        )
    return files


@dataclass
class Repositories:
    customer: Path
    bases: Path
    settings: Settings

    def tree(self, **overrides: str) -> dict[str, str]:
        inputs = {
            "repo_url": REPO_URL,
            "bases_url": BASES_URL,
            "slug": "acme-one",
            "hostname": "argocd.acme-one.k8s.example.test",
            "ingress_vip": "10.42.0.240",
            "interface": "ens3",
            "acme_contact": "noc@example.test",
        }
        return render_tree(**{**inputs, **overrides})

    def preview(self, files: dict[str, str] | None = None, **kwargs: Any) -> dict[str, Any]:
        return prepare_preview(
            repo_url=REPO_URL,
            username=USERNAME,
            token=TOKEN,
            files=self.tree() if files is None else files,
            settings=kwargs.pop("settings", self.settings),
            baseline=kwargs.pop("baseline", {}),
            **kwargs,
        )

    def publish(self, preview: dict[str, Any], **kwargs: Any) -> str:
        return publish_preview(
            repo_url=REPO_URL,
            username=USERNAME,
            token=TOKEN,
            preview=preview,
            operation_id=kwargs.pop("operation_id", "operation-001"),
            settings=kwargs.pop("settings", self.settings),
            **kwargs,
        )

    def seed(self, files: dict[str, str] | None = None) -> str:
        return commit(
            self.customer,
            {
                **(self.tree() if files is None else files),
                "k8s-manifests": ("160000", self.settings.customer_cluster_bases_revision),
            },
        )


@pytest.fixture
def repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Repositories:
    algorithm = getattr(request, "param", "sha1")
    customer, bases = tmp_path / "customer.git", tmp_path / "bases.git"
    for path in (customer, bases):
        git(
            tmp_path,
            "init",
            "--bare",
            "--template=",
            "--initial-branch=main",
            f"--object-format={algorithm}",
            str(path),
        )
    revision = commit(bases, base_files())
    mapping = {REPO_URL: customer, BASES_URL: bases}
    monkeypatch.setattr(gitops_git, "_transport", lambda url: Transport(str(mapping[url]), "file"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return Repositories(
        customer,
        bases,
        Settings(
            customer_cluster_bases_url=BASES_URL,
            customer_cluster_bases_revision=revision,
        ),
    )


@pytest.mark.parametrize("repos", ["sha1", "sha256"], indirect=True)
def test_empty_repo_preview_publish_and_exact_retry(repos: Repositories) -> None:
    preview = repos.preview()
    assert preview["action"] == "initialize"
    assert preview["expected_head"] is None
    assert git(repos.customer, "show-ref", check=False) == ""
    assert preview == json.loads(json.dumps(preview))
    assert preview["validation"]["envoy_protections"] is True
    assert len(preview["validation"]["kustomizations"]) == 6
    sha = repos.publish(preview)
    assert sha == git(repos.customer, "rev-parse", "main")
    assert len(sha) == len(preview["bases_revision"])
    assert git(repos.customer, "show", "-s", "--format=%P", sha) == ""
    assert repos.publish(preview) == sha
    assert git(repos.customer, "rev-list", "--count", "main") == "1"
    assert git(repos.customer, "ls-tree", "main", "k8s-manifests").startswith(
        f"160000 commit {preview['bases_revision']}"
    )
    assert TOKEN not in json.dumps(preview)
    assert TOKEN not in (repos.customer / "config").read_text()
    assert "Customer-GitOps-Operation: operation-001" in git(
        repos.customer, "log", "-1", "--format=%B"
    )


def test_second_cluster_preserves_root_readme_pin_and_other_files(repos: Repositories) -> None:
    repos.seed()
    modules = repos.tree()[".gitmodules"] + f"# private customer note {TOKEN}\n"
    first = commit(
        repos.customer,
        {
            "README.md": f"Customer documentation {TOKEN}\n",
            ".gitmodules": modules,
            "private/credentials.txt": TOKEN,
        },
    )
    updated_settings = replace(repos.settings, customer_cluster_bases_revision="main")
    preview = repos.preview(
        repos.tree(
            slug="acme-two", hostname="argocd.acme-two.k8s.example.test", ingress_vip="10.43.0.240"
        ),
        settings=updated_settings,
    )
    assert preview["action"] == "add"
    assert preview["bases_revision"] == repos.settings.customer_cluster_bases_revision
    assert "README.md" not in preview["files"]
    assert ".gitmodules" not in preview["files"]
    assert TOKEN not in json.dumps(preview)
    sha = repos.publish(preview, settings=updated_settings)
    assert git(repos.customer, "rev-parse", f"{first}:clusters/acme-one") == git(
        repos.customer, "rev-parse", f"{sha}:clusters/acme-one"
    )
    assert git(repos.customer, "show", "main:README.md") == f"Customer documentation {TOKEN}"
    assert git(repos.customer, "show", "main:.gitmodules") == modules.strip()
    assert git(repos.customer, "show", "main:private/credentials.txt") == TOKEN


def test_new_deployment_pin_does_not_upgrade_existing_gitlink(repos: Repositories) -> None:
    files = repos.tree()
    repos.seed()
    new_pin = commit(repos.bases, {"README.md": "Base release notes"})
    settings = replace(repos.settings, customer_cluster_bases_revision=new_pin)
    preview = repos.preview(files, baseline=files, settings=settings)
    assert preview["action"] == "noop"
    assert preview["bases_revision"] != new_pin
    sha = repos.publish(preview, settings=settings)
    assert (
        git(repos.customer, "rev-parse", f"{sha}:k8s-manifests")
        == repos.settings.customer_cluster_bases_revision
    )


@pytest.mark.parametrize("branch", ["trunk", "tags-only"])
def test_nonempty_without_main_is_never_treated_as_empty(
    repos: Repositories, branch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = commit(repos.customer, {"README.md": "Existing customer repository"}, branch="trunk")
    if branch == "tags-only":
        git(repos.customer, "update-ref", "refs/tags/v1", sha)
        git(repos.customer, "update-ref", "-d", "refs/heads/trunk")
    commands = []
    original = gitops_git._run

    def record(args: list[str], **kwargs: Any) -> Any:
        commands.append(args[0])
        return original(args, **kwargs)

    monkeypatch.setattr(gitops_git, "_run", record)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview()
    assert error.value.code == "branch_missing"
    assert "clone" not in commands
    assert git(repos.customer, "rev-parse", "--verify", "refs/heads/main", check=False) == ""


def test_readme_only_repository_is_supported(repos: Repositories) -> None:
    commit(repos.customer, {"README.md": "Customer introduction\n"})
    preview = repos.preview()
    assert preview["action"] == "add"
    repos.publish(preview)
    assert git(repos.customer, "show", "main:README.md") == "Customer introduction"


def test_existing_compatible_tree_requires_explicit_adoption(repos: Repositories) -> None:
    repos.seed()
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview()
    assert error.value.code == "adoption_required"
    preview = repos.preview(adopt=True)
    assert preview["action"] == "adopt"
    assert preview["diff"] == ""
    head = git(repos.customer, "rev-parse", "main")
    assert repos.publish(preview) == head
    assert repos.preview(baseline=preview["files"])["action"] == "noop"


def test_equivalent_repository_identity_is_adopted_and_recorded_canonically(
    repos: Repositories,
) -> None:
    current = repos.tree(repo_url="https://forgejo.example.test/Customer/Clusters.GIT")
    current["README.md"] = "Customer-maintained introduction\n"
    before = repos.seed(current)
    preview = repos.preview(adopt=True)
    assert preview["action"] == "adopt"
    assert preview["diff"] == ""
    assert "README.md" not in preview["files"]
    sha = repos.publish(preview)
    assert git(repos.customer, "show", "-s", "--format=%P", sha) == before
    for path, content in preview["files"].items():
        assert content == repos.tree()[path]
        assert git(repos.customer, "show", f"{sha}:{path}") == content.strip()
    assert git(repos.customer, "show", "main:README.md") == "Customer-maintained introduction"
    assert repos.preview(baseline=preview["files"])["action"] == "noop"
    assert repos.publish(preview) == sha


def test_equivalent_source_spelling_is_harmless_on_retry(repos: Repositories) -> None:
    preview = repos.preview()
    repos.publish(preview)
    latest = commit(
        repos.customer,
        {
            f"{APPS}/argocd.yaml": repos.tree(
                repo_url="https://forgejo.example.test/CUSTOMER/CLUSTERS.git"
            )[f"{APPS}/argocd.yaml"],
        },
    )
    assert repos.publish(preview) == latest


@pytest.mark.parametrize(
    "path,old,new",
    [
        (f"{APPS}/argocd.yaml", REPO_URL, "https://forgejo.example.test/other/customer.git"),
        (
            f"{APPS}/customer-cluster-apps.yaml",
            "clusters/acme-one/argocd-apps",
            "clusters/other/argocd-apps",
        ),
        (f"{APPS}/argocd-ingress.yaml", "clusters/acme-one/addons", "clusters/other/addons"),
        (f"{INGRESS}/gateway.yaml", "argocd.acme-one", "argocd.other"),
        (f"{INGRESS}/certificate.yaml", "argocd.acme-one", "argocd.other"),
        (f"{INGRESS}/routes.yaml", "argocd.acme-one", "argocd.other"),
    ],
)
def test_adoption_rejects_cross_customer_values(
    repos: Repositories, path: str, old: str, new: str
) -> None:
    tree = repos.tree()
    tree[path] = tree[path].replace(old, new)
    repos.seed(tree)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(adopt=True)
    assert error.value.code == "incompatible_adoption"


def test_partial_compatible_tree_can_be_adopted(repos: Repositories) -> None:
    commit(repos.customer, {f"{INGRESS}/gateway.yaml": repos.tree()[f"{INGRESS}/gateway.yaml"]})
    preview = repos.preview(adopt=True)
    assert preview["action"] == "adopt"
    repos.publish(preview)
    assert git(repos.customer, "show", f"main:{APPS}/argocd.yaml")


def test_update_and_disjoint_manual_changes(repos: Repositories) -> None:
    baseline = repos.tree()
    repos.seed()
    manual = repos.tree(acme_contact="customer-noc@example.test")
    commit(
        repos.customer,
        {f"{INGRESS}/issuer.yaml": manual[f"{INGRESS}/issuer.yaml"], "private.txt": TOKEN},
    )
    preview = repos.preview(repos.tree(interface="ens4"), baseline=baseline)
    assert preview["action"] == "update"
    assert "customer-noc@example.test" in preview["files"][f"{INGRESS}/issuer.yaml"]
    assert "private.txt" not in preview["diff"]
    assert TOKEN not in json.dumps(preview)
    head = repos.publish(preview)
    assert (
        git(repos.customer, "diff-tree", "--no-commit-id", "--name-only", "-r", head)
        == f"{INGRESS}/cilium-load-balancer.yaml"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "argocd.manual-name.k8s.example.test"),
        ("ingress_vip", "10.42.0.241"),
        ("interface", "ens4"),
    ],
)
def test_coherent_manual_authoritative_input_changes_conflict_with_unchanged_proposal(
    repos: Repositories,
    field: str,
    value: str,
) -> None:
    baseline = repos.tree()
    repos.seed()
    manual = repos.tree(**{field: value})
    # Every occurrence is coherent and the tree still passes schema validation.
    # Authority must come from the operator's proposal rather than that coherence.
    assert getattr(validate_tree(manual, REPO_URL, BASES_URL), field) == value
    changed_head = commit(repos.customer, manual)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(baseline=baseline)
    assert error.value.code == "manual_conflict"
    assert git(repos.customer, "rev-parse", "main") == changed_head
    assert git(repos.customer, "rev-list", "--count", "main") == "2"


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "argocd.approved-name.k8s.example.test"),
        ("ingress_vip", "10.42.0.241"),
        ("interface", "ens4"),
    ],
)
def test_authoritative_changes_from_the_operator_proposal_can_be_published(
    repos: Repositories,
    field: str,
    value: str,
) -> None:
    baseline = repos.tree()
    previous_head = repos.seed()
    proposed = repos.tree(**{field: value})
    preview = repos.preview(proposed, baseline=baseline)
    assert preview["action"] == "update"
    assert validate_tree(preview["files"], REPO_URL, BASES_URL) == validate_tree(
        proposed,
        REPO_URL,
        BASES_URL,
    )
    sha = repos.publish(preview)
    assert git(repos.customer, "show", "-s", "--format=%P", sha) == previous_head


def test_manual_acme_contact_is_preserved_and_exported_for_unchanged_proposal(
    repos: Repositories,
) -> None:
    baseline = repos.tree()
    repos.seed()
    manual = repos.tree(acme_contact="customer-noc@example.test")
    changed_head = commit(
        repos.customer,
        {f"{INGRESS}/issuer.yaml": manual[f"{INGRESS}/issuer.yaml"]},
    )
    preview = repos.preview(baseline=baseline)
    assert preview["action"] == "noop"
    assert "validate_tree" in customer_gitops.__all__
    inputs = validate_tree(preview["files"], preview["repo_url"], preview["bases_url"])
    expected = validate_tree(baseline, REPO_URL, BASES_URL)
    assert inputs.acme_contact == "customer-noc@example.test"
    assert replace(inputs, acme_contact=expected.acme_contact) == expected
    assert repos.publish(preview) == changed_head
    assert git(repos.customer, "rev-list", "--count", "main") == "2"


def test_overlapping_manual_update_conflicts(repos: Repositories) -> None:
    baseline = repos.tree()
    repos.seed()
    manual = repos.tree(acme_contact="customer@example.test")
    commit(repos.customer, {f"{INGRESS}/issuer.yaml": manual[f"{INGRESS}/issuer.yaml"]})
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(repos.tree(acme_contact="operator@example.test"), baseline=baseline)
    assert error.value.code == "manual_conflict"


def test_manual_deletion_is_not_recreated_silently(repos: Repositories) -> None:
    baseline = repos.tree()
    repos.seed()
    commit(repos.customer, {}, remove=(f"{INGRESS}/issuer.yaml",))
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(baseline=baseline)
    assert error.value.code == "manual_conflict"


def test_comments_and_formatting_do_not_leak_into_previews(repos: Repositories) -> None:
    tree = repos.tree()
    tree[f"{INGRESS}/issuer.yaml"] = f"# {TOKEN}\n" + _yaml(
        yaml.safe_load(tree[f"{INGRESS}/issuer.yaml"])
    )
    repos.seed(tree)
    preview = repos.preview(adopt=True)
    assert TOKEN not in json.dumps(preview)
    assert preview["diff"] == ""
    assert f"{INGRESS}/issuer.yaml" in preview["formatting_paths"]
    repos.publish(preview)
    assert git(repos.customer, "show", f"main:{INGRESS}/issuer.yaml") == (
        preview["files"][f"{INGRESS}/issuer.yaml"].strip()
    )


def test_owned_old_schema_can_be_upgraded_without_exposing_removed_fields(
    repos: Repositories,
) -> None:
    baseline = repos.tree()
    doc = yaml.safe_load(baseline[f"{INGRESS}/envoy-proxy.yaml"])
    del doc["spec"]["provider"]["kubernetes"]["envoyDeployment"]["pod"]
    doc["metadata"]["annotations"] = {"legacy-note": TOKEN}
    baseline[f"{INGRESS}/envoy-proxy.yaml"] = _yaml(doc)
    repos.seed(baseline)
    preview = repos.preview(baseline=baseline)
    assert preview["action"] == "update"
    assert TOKEN not in json.dumps(preview)
    assert "existing contents withheld" in preview["diff"]
    repos.publish(preview)
    doc = yaml.safe_load(git(repos.customer, "show", f"main:{INGRESS}/envoy-proxy.yaml"))
    assert doc["spec"]["provider"]["kubernetes"]["envoyDeployment"]["pod"][
        "topologySpreadConstraints"
    ]


def test_disjoint_manual_change_cannot_remove_topology_protection(repos: Repositories) -> None:
    baseline = repos.tree()
    repos.seed()
    doc = yaml.safe_load(baseline[f"{INGRESS}/envoy-proxy.yaml"])
    del doc["spec"]["provider"]["kubernetes"]["envoyDeployment"]["pod"]
    commit(repos.customer, {f"{INGRESS}/envoy-proxy.yaml": _yaml(doc)})
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(repos.tree(acme_contact="new@example.test"), baseline=baseline)
    assert error.value.code == "manual_conflict"


@pytest.mark.parametrize("overlap", [False, True])
def test_stale_preview_rejects_head_movement(repos: Repositories, overlap: bool) -> None:
    preview = repos.preview()
    if overlap:
        commit(
            repos.customer,
            {
                f"{INGRESS}/issuer.yaml": repos.tree(acme_contact="manual@example.test")[
                    f"{INGRESS}/issuer.yaml"
                ]
            },
        )
    else:
        commit(repos.customer, {"notes.txt": "A harmless change"})
    before = git(repos.customer, "rev-parse", "main")
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview)
    assert error.value.code == ("manual_conflict" if overlap else "stale_preview")
    assert git(repos.customer, "rev-parse", "main") == before


def test_exact_retry_succeeds_after_harmless_head_advance(repos: Repositories) -> None:
    preview = repos.preview()
    repos.publish(preview)
    latest = commit(
        repos.customer, {"notes.txt": "Customer update", "README.md": "Revised introduction"}
    )
    assert repos.publish(preview) == latest
    assert git(repos.customer, "rev-list", "--count", "main") == "2"


@pytest.mark.parametrize("advance", [False, True])
def test_lost_push_response_is_confirmed(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch, advance: bool
) -> None:
    preview = repos.preview()
    original = GitRepository.push

    def lose_response(self: GitRepository, sha: str, expected: str | None) -> bool:
        assert original(self, sha, expected)
        if advance:
            commit(repos.customer, {"notes.txt": "Immediately following commit"})
        raise CustomerGitOpsError("Lost response", "push_failed")

    monkeypatch.setattr(GitRepository, "push", lose_response)
    sha = repos.publish(preview)
    assert sha == git(repos.customer, "rev-parse", "main")
    assert repos.publish(preview) == sha


def test_rejected_push_fails_without_exposing_transport_output(repos: Repositories) -> None:
    preview = repos.preview()
    hook = repos.customer / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\nprintf '%s\\n' '{TOKEN}' >&2\nexit 1\n")
    hook.chmod(0o700)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview)
    assert error.value.code == "push_failed"
    assert TOKEN not in str(error.value)
    assert git(repos.customer, "show-ref", check=False) == ""


def test_push_success_status_without_remote_update_is_not_success(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = repos.preview()
    monkeypatch.setattr(GitRepository, "push", lambda *args: True)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview)
    assert error.value.code == "push_unconfirmed"


def test_push_freshness_preserves_racing_commit(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = repos.preview()
    original = GitRepository.push
    raced = []

    def race(self: GitRepository, sha: str, expected: str | None) -> bool:
        raced.append(commit(repos.customer, {"notes.txt": "External writer won"}))
        return original(self, sha, expected)

    monkeypatch.setattr(GitRepository, "push", race)
    with pytest.raises(CustomerGitOpsError):
        repos.publish(preview)
    assert git(repos.customer, "rev-parse", "main") == raced[0]
    assert git(repos.customer, "rev-list", "--count", "main") == "1"


@pytest.mark.parametrize("initial", [False, True])
def test_plain_push_rejects_a_writer_racing_after_the_freshness_check(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
) -> None:
    baseline = {} if initial else repos.tree()
    if not initial:
        repos.seed()
    preview = repos.preview(repos.tree(acme_contact="updated@example.test"), baseline=baseline)
    original = gitops_git._run
    raced = []

    def race(args: list[str], **kwargs: Any) -> Any:
        if args[0] == "push":
            assert not any(arg.startswith("--force") or arg == "-f" for arg in args)
            assert not args[-1].startswith("+")
            raced.append(commit(repos.customer, {"notes.txt": "Raced after the freshness read"}))
        return original(args, **kwargs)

    monkeypatch.setattr(gitops_git, "_run", race)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview)
    assert error.value.code == "push_failed"
    assert len(raced) == 1
    assert git(repos.customer, "rev-parse", "main") == raced[0]


@pytest.mark.parametrize(
    "candidate_kind", ["rewind", "orphan", "ancestor", "unrelated", "merge", "tree"]
)
def test_push_rejects_any_commit_without_exact_single_expected_parent(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    ancestor = repos.seed()
    expected = commit(repos.customer, {"notes.txt": "Keep this customer commit"})
    with tempfile.TemporaryDirectory() as temporary:
        client, head = GitRepository.customer(Path(temporary), REPO_URL, USERNAME, TOKEN)
        assert head == expected
        tree = git(client.path, "rev-parse", "main^{tree}")
        orphan = git(client.path, "commit-tree", tree, data="Unrelated fixture root\n")
        parents = {
            "ancestor": [ancestor],
            "unrelated": [orphan],
            "merge": [expected, orphan],
        }
        if candidate_kind in parents:
            arguments = [arg for parent in parents[candidate_kind] for arg in ("-p", parent)]
            candidate = git(
                client.path, "commit-tree", tree, *arguments, data="Invalid ancestry\n"
            )
        else:
            candidate = {"rewind": ancestor, "orphan": orphan, "tree": tree}[candidate_kind]
        commands = []
        original = gitops_git._run

        def record(args: list[str], **kwargs: Any) -> Any:
            commands.append(args[0])
            return original(args, **kwargs)

        monkeypatch.setattr(gitops_git, "_run", record)
        with pytest.raises(CustomerGitOpsError) as error:
            client.push(candidate, expected)
        assert error.value.code == "invalid_commit"
        assert "push" not in commands
        assert git(repos.customer, "rev-parse", "main") == expected


def test_empty_repository_push_rejects_a_non_root_commit(repos: Repositories) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        client, head = GitRepository.customer(Path(temporary), REPO_URL, USERNAME, TOKEN)
        assert head is None
        tree = git(client.path, "mktree", data="")
        parent = git(client.path, "commit-tree", tree, data="Unrelated fixture history\n")
        child = git(client.path, "commit-tree", tree, "-p", parent, data="Unexpected history\n")
        with pytest.raises(CustomerGitOpsError) as error:
            client.push(child, None)
        assert error.value.code == "invalid_commit"
        assert git(repos.customer, "show-ref", check=False) == ""


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        ".gitmodules",
        "k8s-manifests",
        "clusters",
        "clusters/acme-one",
        "clusters/acme-one/addons",
        INGRESS,
        f"{INGRESS}/README.md",
        f"{INGRESS}/issuer.yaml",
        APPS,
    ],
)
def test_managed_symlinks_and_ancestors_are_rejected(repos: Repositories, path: str) -> None:
    commit(repos.customer, {path: ("120000", "../../outside")})
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(adopt=True)
    assert error.value.code in {"unsafe_repository", "repository_unavailable"}


@pytest.mark.parametrize(
    "files",
    [
        {"k8s-manifests/README.md": "A directory, not a gitlink"},
        {f"{INGRESS}/issuer.yaml": ("100755", "executable")},
        {f"{INGRESS}/issuer.yaml/child": "Directory instead of manifest"},
        {"unexpected-submodule": ("160000", "a" * 40)},
        {"k8s-manifests": ("160000", "a" * 40)},
        {".gitmodules": '[submodule "k8s-manifests"]\npath = k8s-manifests\n'},
    ],
)
def test_unsafe_gitlink_shapes(repos: Repositories, files: dict[str, Any]) -> None:
    commit(repos.customer, files)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(adopt=True)
    assert error.value.code == "unsafe_repository"


@pytest.mark.parametrize(
    "extra",
    [
        "\tupdate = !touch /tmp/unsafe\n",
        "\tbranch = main\n",
        "\tpath = ../escape\n",
        "[include]\n\tpath = /etc/gitconfig\n",
        '[submodule "other"]\n\tpath = other\n\turl = /tmp/other\n',
        "\turl = https://attacker.example.test/bases.git\n",
    ],
)
def test_unsafe_gitmodules_config_is_not_followed(repos: Repositories, extra: str) -> None:
    tree = repos.tree()
    tree[".gitmodules"] += extra
    repos.seed(tree)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(adopt=True)
    assert error.value.code in {"unsafe_repository", "repository_unavailable"}


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/tmp/escape",
        ".git/config",
        "clusters/acme-one/../escape",
        "clusters/acme-one/secret.yaml",
        "clusters/other/argocd-apps/argocd.yaml",
    ],
)
def test_only_known_generated_paths_are_accepted(repos: Repositories, path: str) -> None:
    with pytest.raises(CustomerGitOpsError):
        repos.preview({**repos.tree(), path: "unsafe"})


def test_ambient_auth_hooks_filters_and_public_credentials_are_isolated(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = tmp_path / "hook-ran"
    hostile_config = tmp_path / "hostile.gitconfig"
    hostile_config.write_text(
        f'[core]\n hooksPath = {tmp_path}\n[filter "danger"]\n smudge = touch {sentinel}\n'
    )
    (tmp_path / "post-checkout").write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    (tmp_path / "post-checkout").chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_config))
    monkeypatch.setenv("GIT_TRACE", str(sentinel))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"Authorization: Bearer {TOKEN}")
    commit(
        repos.customer,
        {
            ".gitattributes": "* filter=danger\n",
            ".gitconfig": hostile_config.read_text(),
            "unrelated-link": ("120000", str(sentinel)),
        },
    )
    calls = []
    original = gitops_git._run

    def record(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, dict(kwargs["env"])))
        return original(args, **kwargs)

    monkeypatch.setattr(gitops_git, "_run", record)
    repos.publish(repos.preview())
    assert not sentinel.exists()
    encoded = base64.b64encode(f"{USERNAME}:{TOKEN}".encode()).decode()
    customer_calls = [(args, env) for args, env in calls if str(repos.customer) in args]
    base_calls = [(args, env) for args, env in calls if str(repos.bases) in args]
    assert customer_calls and base_calls
    for args, env in customer_calls:
        assert TOKEN not in " ".join(args) and encoded not in " ".join(args)
        assert f"http.{REPO_URL}.extraHeader" in env.values()
        assert "GIT_TRACE" not in env
        assert f"Authorization: Basic {encoded}" in env.values()
    for _, env in base_calls:
        assert encoded not in json.dumps(env) and TOKEN not in json.dumps(env)
        assert not any(value.startswith("Authorization:") for value in env.values())


@pytest.mark.parametrize(
    "revision", ["", "main", "v1.0", "a" * 39, "a" * 41, "0" * 40, "b" * 63, "b" * 65]
)
def test_initial_pin_must_be_a_full_immutable_sha(repos: Repositories, revision: str) -> None:
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(settings=replace(repos.settings, customer_cluster_bases_revision=revision))
    assert error.value.code == "invalid_bases_revision"


def test_missing_kustomize_fails_closed(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitops_validate.shutil, "which", lambda name: None)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview()
    assert error.value.code == "kustomize_missing"
    assert git(repos.customer, "show-ref", check=False) == ""


@pytest.mark.parametrize(
    "bad",
    ["pruning", "topology", "crd", "remote", "symlink", "gitlink", "missing-base", "invalid-yaml"],
)
def test_unsafe_pinned_bases_fail_validation(repos: Repositories, bad: str) -> None:
    files = base_files()
    remove: tuple[str, ...] = ()
    if bad == "pruning":
        doc = yaml.safe_load(files["envoy-gateway/kustomization.yaml"])
        del doc["patches"]
        changes = {"envoy-gateway/kustomization.yaml": _yaml(doc)}
    elif bad in {"topology", "crd"}:
        docs = list(yaml.safe_load_all(files["envoy-gateway/resources.yaml"]))
        if bad == "topology":
            doc = next(doc for doc in docs if doc["kind"] == "Deployment")
            doc["spec"]["template"]["spec"]["topologySpreadConstraints"] = []
        else:
            docs = [doc for doc in docs if doc["kind"] != "CustomResourceDefinition"]
        changes = {"envoy-gateway/resources.yaml": yaml.safe_dump_all(docs)}
    elif bad == "remote":
        doc = yaml.safe_load(files["envoy-gateway/kustomization.yaml"])
        doc["resources"] = ["https://attacker.example.test/manifest.yaml"]
        changes = {"envoy-gateway/kustomization.yaml": _yaml(doc)}
    elif bad == "symlink":
        changes = {"envoy-gateway/external.yaml": ("120000", "/etc/passwd")}
    elif bad == "gitlink":
        changes = {"nested": ("160000", "a" * 40)}
    elif bad == "missing-base":
        changes = {}
        remove = ("portal-access/kustomization.yaml",)
    else:
        changes = {"argocd/namespace.yaml": "[invalid YAML"}
    revision = commit(repos.bases, changes, remove=remove)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview(settings=replace(repos.settings, customer_cluster_bases_revision=revision))
    assert error.value.code in {"unsafe_bases", "kustomize_failed"}


def test_preview_cannot_be_changed_or_rebound(repos: Repositories) -> None:
    preview = repos.preview()
    preview["files"][f"{INGRESS}/issuer.yaml"] = repos.tree(acme_contact="attacker@example.test")[
        f"{INGRESS}/issuer.yaml"
    ]
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview)
    assert error.value.code == "invalid_preview"


def test_transport_failures_are_sanitized_and_never_inferred_empty(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = gitops_git.subprocess.run

    def fail(args: list[str], **kwargs: Any) -> Any:
        if args[:2] == ["git", "ls-remote"]:
            kwargs["stdout"].write(f"remote branch main not found {TOKEN}".encode())
            return subprocess.CompletedProcess(args, 128)
        return original(args, **kwargs)

    monkeypatch.setattr(gitops_git.subprocess, "run", fail)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.preview()
    assert error.value.code == "repository_unavailable"
    assert TOKEN not in str(error.value)
