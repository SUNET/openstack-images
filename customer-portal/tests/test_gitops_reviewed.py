"""Optional local parity checks against the reviewed checkout; all Git reads are local."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app import gitops_git
from app.config import Settings
from app.customer_gitops import prepare_preview, publish_preview, render_tree
from app.gitops_git import Transport
from app.gitops_render import APPLICATIONS, INGRESS_RESOURCES
from app.gitops_validate import validate_kustomize
from app.repository_schemas import canonical_repository_url
from tests.test_gitops_publisher import BASES_URL, TOKEN, USERNAME, commit, git

SOURCE = Path("/home/micke/sources/vdc/customer-sunet-clusters-test")
pytestmark = pytest.mark.skipif(
    not (SOURCE / "k8s-manifests/envoy-gateway/kustomization.yaml").is_file(),
    reason="Reviewed local checkout is not installed",
)


def _tree() -> dict[str, str]:
    return render_tree(
        repo_url="https://platform.sunet.se/VDC/customer-sunet-clusters-test.git",
        slug="sunet-two",
        hostname="argocd.sunet-two.k8s-test.sunetvdc.se",
        ingress_vip="10.42.0.240",
        interface="ens3",
        acme_contact="noc@sunet.se",
        bases_url=BASES_URL,
    )


def test_objects_match_the_reviewed_sources() -> None:
    files = _tree()
    paths = [f"clusters/sunet-two/addons/argocd-ingress/{name}" for name in INGRESS_RESOURCES]
    paths.extend(f"clusters/sunet-two/argocd-apps/{name}.yaml" for name in APPLICATIONS)
    for path in paths:
        assert list(yaml.safe_load_all(files[path])) == list(
            yaml.safe_load_all((SOURCE / path).read_text(encoding="utf-8"))
        ), path


def test_real_pinned_base_passes_offline_runtime_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = SOURCE / "k8s-manifests"
    revision = git(base, "rev-parse", "HEAD")
    monkeypatch.setattr(
        gitops_git,
        "_transport",
        lambda url: Transport(str({BASES_URL: base}[url]), "file"),
    )
    result = validate_kustomize(tmp_path, _tree(), BASES_URL, revision)
    assert result["envoy_protections"] is True
    assert len(result["kustomizations"]) == 6


def test_adopt_reviewed_uppercase_vdc_manifests_into_canonical_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = SOURCE / "k8s-manifests"
    revision = git(base, "rev-parse", "HEAD")
    repo_url = canonical_repository_url(
        "https://platform.sunet.se/VDC/customer-sunet-clusters-test.git"
    )
    customer = tmp_path / "customer.git"
    git(tmp_path, "init", "--bare", "--template=", "--initial-branch=main", str(customer))
    files = _tree()
    for path in files:
        if path.endswith(".yaml"):
            files[path] = (SOURCE / path).read_text(encoding="utf-8")
    apps = "clusters/sunet-two/argocd-apps"
    kustomization = yaml.safe_load(files[f"{apps}/kustomization.yaml"])
    kustomization["resources"].remove("sealed-secrets.yaml")
    files[f"{apps}/kustomization.yaml"] = yaml.safe_dump(kustomization)
    files["README.md"] = "Customer-owned repository documentation\n"
    initial = commit(customer, {**files, "k8s-manifests": ("160000", revision)})
    mapping = {repo_url: customer, BASES_URL: base}
    monkeypatch.setattr(gitops_git, "_transport", lambda url: Transport(str(mapping[url]), "file"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    settings = Settings(
        customer_cluster_bases_url=BASES_URL, customer_cluster_bases_revision=revision
    )
    proposed = render_tree(
        repo_url=repo_url,
        slug="sunet-two",
        hostname="argocd.sunet-two.k8s-test.sunetvdc.se",
        ingress_vip="10.42.0.240",
        interface="ens3",
        acme_contact="noc@sunet.se",
        bases_url=BASES_URL,
    )
    preview = prepare_preview(
        repo_url=repo_url,
        username=USERNAME,
        token=TOKEN,
        files=proposed,
        baseline={},
        settings=settings,
        adopt=True,
    )
    assert preview["action"] == "adopt"
    assert preview["expected_head"] == initial
    assert preview["repo_url"] == repo_url
    assert "README.md" not in preview["files"]
    sha = publish_preview(
        repo_url=repo_url,
        username=USERNAME,
        token=TOKEN,
        preview=preview,
        operation_id="reviewed-adoption",
        settings=settings,
    )
    assert git(customer, "show", "-s", "--format=%P", sha) == initial
    assert git(customer, "show", "main:README.md") == "Customer-owned repository documentation"
    assert git(customer, "rev-parse", "main:k8s-manifests") == revision
    for application in APPLICATIONS:
        path = f"{apps}/{application}.yaml"
        recorded = preview["files"][path]
        assert recorded == proposed[path]
        assert yaml.safe_load(recorded)["spec"]["source"]["repoURL"] == repo_url
        assert git(customer, "show", f"main:{path}") == recorded.strip()
    assert (
        prepare_preview(
            repo_url=repo_url,
            username=USERNAME,
            token=TOKEN,
            files=proposed,
            baseline=preview["files"],
            settings=settings,
        )["action"]
        == "noop"
    )
