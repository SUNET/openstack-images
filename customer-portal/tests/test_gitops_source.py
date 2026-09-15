"""Operator-owned inputs: exact inventory revision, allocation and readiness gates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from app import gitops_source, k8s
from app.cluster_git_backend import ClusterGitBackend
from app.config import Settings
from app.customer_gitops import CustomerGitOpsError
from app.models import Contract, Customer, TenantCluster

INVENTORY_COMMIT = "a1" * 20
SOURCE_SECRET = "source-secret-must-not-be-projected"
NAMESPACE = "customer-clusters-test"


def existing_cluster(slug: str = "eosc-one") -> TenantCluster:
    return TenantCluster(
        name="Existing EOSC cluster", slug=slug,
        contract=Contract(
            contract_number="EOSC-2026", customer=Customer(name="EOSC", domain="eosc.test")
        ),
        created_by_sub="admin@test", openbao_mount=f"kubernetes/{slug}",
        management_project_resource_name="eosc-management",
        argocd_alias="display-alias.eosc.test",
    )


@dataclass
class ManagedSource:
    """Fake Kubernetes transport and immutable management-repository object store."""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshots: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = field(
        default_factory=dict
    )
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    def add(self, cluster: TenantCluster, *, commit: str = INVENTORY_COMMIT) -> None:
        slug = cluster.slug
        spec = {
            "contractNumber": cluster.contract.contract_number,
            "customerDomain": cluster.contract.customer.domain,
            "workerGroups": 2,
            "profileRef": {"name": "standard-v1"},
            "openstack": {
                "projectName": "EOSC-management",
                "projectResourceName": cluster.management_project_resource_name,
            },
            "dns": {"argocdHostname": f"argocd.{slug}.operator.test"},
            "unrelatedCredential": SOURCE_SECRET,
        }
        declaration = {
            "apiVersion": "customer-clusters.sunet.se/v1alpha1",
            "kind": "ManagedCluster", "metadata": {"name": slug}, "spec": deepcopy(spec),
        }
        inventory = {"all": {"vars": {
            "customer_cluster_ingress_vip": "10.42.0.240",
            "customer_cluster_ingress_floating_ip": "192.0.2.40",
            "customer_cluster_api_vip": "10.42.0.239",
            "customer_cluster_argocd_hostname": "untrusted-inventory-host.test",
            "ansible_password": SOURCE_SECRET,
        }}}
        self.snapshots[slug, commit] = declaration, inventory
        self.documents[slug] = {
            "kind": "ManagedCluster",
            "metadata": {"name": slug, "namespace": NAMESPACE, "generation": 7, "uid": slug},
            "spec": spec,
            "status": {
                "phase": "VirtualMachinesReady", "observedGeneration": 7,
                "conditions": [{"type": "Ready", "status": "True", "reason": "Verified"}],
                "inventoryPath": f"clusters/{slug}/generated/ansible/hosts.yml",
                "inventoryCommit": commit, "ingressFloatingIp": "192.0.2.40",
            },
        }

    def get_namespaced_custom_object(self, **kwargs: Any) -> dict[str, Any]:
        self.api_calls.append(kwargs)
        assert kwargs == {
            "group": "customer-clusters.sunet.se", "version": "v1alpha1",
            "namespace": NAMESPACE, "plural": "managedclusters", "name": kwargs["name"],
            "_request_timeout": 10,
        }
        if self.error is not None:
            raise self.error
        return deepcopy(self.documents[kwargs["name"]])

    def read_cluster_snapshot(self, slug: str, commit: str) -> tuple[dict, dict]:
        self.reads.append((slug, commit))
        try:
            return deepcopy(self.snapshots[slug, commit])
        except KeyError:
            raise ValueError("Commit unavailable") from None


@pytest.fixture
def source_settings() -> Settings:
    return Settings(
        managed_cluster_namespace=NAMESPACE, cluster_environment="test",
        cluster_dns_zone="new-deployment-zone.test", customer_cluster_node_interface="ens7",
    )


@pytest.fixture
def source_cluster() -> TenantCluster:
    return existing_cluster()


@pytest.fixture
def managed_source(
    monkeypatch: pytest.MonkeyPatch, source_cluster: TenantCluster,
) -> ManagedSource:
    source = ManagedSource()
    source.add(source_cluster)
    monkeypatch.setattr(k8s, "_api", source)
    return source


async def test_snapshot_uses_exact_status_commit_and_allowlists_authoritative_values(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
) -> None:
    # A newer branch inventory is deliberately different from the operator-verified commit.
    newer = deepcopy(managed_source.snapshots[source_cluster.slug, INVENTORY_COMMIT])
    newer[1]["all"]["vars"]["customer_cluster_ingress_vip"] = "10.99.0.240"
    managed_source.snapshots[source_cluster.slug, "b2" * 20] = newer
    snapshot = await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert managed_source.reads == [("eosc-one", INVENTORY_COMMIT)]
    assert snapshot.as_dict() == {
        "namespace": NAMESPACE, "slug": "eosc-one", "uid": "eosc-one", "generation": 7,
        "inventory_commit": INVENTORY_COMMIT,
        "inventory_path": "clusters/eosc-one/generated/ansible/hosts.yml",
        "hostname": "argocd.eosc-one.operator.test", "ingress_vip": "10.42.0.240",
        "interface": "ens7",
    }
    assert SOURCE_SECRET not in str(snapshot.as_dict())
    assert snapshot.hostname != source_cluster.argocd_alias
    assert snapshot.ingress_vip != "192.0.2.40"


@pytest.mark.parametrize("status,code", [(401, "forbidden"), (403, "forbidden"),
                                        (404, "missing"), (500, "unavailable")])
async def test_kubernetes_failure_is_distinct_from_absence_and_never_echoes_body(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    status: int, code: str,
) -> None:
    error = ApiException(status=status, reason=SOURCE_SECRET)
    error.body = SOURCE_SECRET
    managed_source.error = error
    observed = await gitops_source.observe(source_cluster.slug, source_settings)
    assert observed["status"] == observed["reason"] == code
    assert observed["inventory_commit"] is None and observed["inventory_path"] is None
    assert SOURCE_SECRET not in str(observed)
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == code and SOURCE_SECRET not in str(caught.value)
    assert not managed_source.reads


@pytest.mark.parametrize("change", [
    {"observedGeneration": 6}, {"observedGeneration": "7"},
    {"phase": "Provisioning"}, {"conditions": []},
    {"conditions": [{"type": "Ready", "status": "False"}]},
    {"conditions": [{"type": "Ready", "status": True}]},
    {"conditions": [{"type": "Available", "status": "True"}]},
])
async def test_current_generation_must_be_verified_before_inventory_is_read(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    change: dict[str, Any],
) -> None:
    managed_source.documents[source_cluster.slug]["status"].update(change)
    observed = await gitops_source.observe(source_cluster.slug, source_settings)
    assert observed["status"] == "pending"
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "inventory_pending" and not managed_source.reads


@pytest.mark.parametrize("generation", [0, -1, True, "7", None])
async def test_invalid_generation_cannot_be_ready(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    generation: Any,
) -> None:
    document = managed_source.documents[source_cluster.slug]
    document["metadata"]["generation"] = generation
    document["status"]["observedGeneration"] = generation
    observation = await gitops_source.observe(source_cluster.slug, source_settings)
    assert observation["status"] == "pending"


@pytest.mark.parametrize("path,value", [
    (("metadata", "name"), "another-cluster"),
    (("metadata", "namespace"), "customer-clusters-prod"),
    (("metadata",), "not-an-object"), (("status",), "not-an-object"),
    (("status", "conditions"), {"Ready": "True"}),
])
async def test_malformed_or_cross_namespace_object_fails_closed(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    path: tuple[str, ...], value: Any,
) -> None:
    document = managed_source.documents[source_cluster.slug]
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert (await gitops_source.observe(source_cluster.slug, source_settings))["status"] == (
        "invalid_source"
    )
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "invalid_source" and not managed_source.reads


@pytest.mark.parametrize("field,value", [
    ("contractNumber", "OTHER-2026"), ("customerDomain", "another-customer.test"),
    ("openstack", {"projectResourceName": "another-project"}),
])
async def test_even_matching_live_and_historical_specs_must_match_portal_ownership(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    field: str, value: Any,
) -> None:
    managed_source.documents[source_cluster.slug]["spec"][field] = value
    declaration, _ = managed_source.snapshots[source_cluster.slug, INVENTORY_COMMIT]
    declaration["spec"][field] = value
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize("field,value", [
    ("contractNumber", "OLD-2025"), ("customerDomain", "old-customer.test"),
    ("openstack", {"projectResourceName": "old-project"}), ("workerGroups", 1),
    ("profileRef", {"name": "different-profile"}),
    ("dns", {"argocdHostname": "old-hostname.test"}),
])
async def test_historical_declaration_must_match_live_allocation(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    field: str, value: Any,
) -> None:
    declaration, _ = managed_source.snapshots[source_cluster.slug, INVENTORY_COMMIT]
    declaration["spec"][field] = value
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize("vip", [
    "192.0.2.40", "10.42.0.239", "8.8.8.8", "127.0.0.1", "169.254.1.1",
    "0.0.0.0", "224.0.0.1", "fd00::1", "10.42.0.240/24", "not-an-ip", None,
])
async def test_ingress_uses_a_private_distinct_vip_not_floating_ip_or_api_vip(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    vip: str | None,
) -> None:
    _, inventory = managed_source.snapshots[source_cluster.slug, INVENTORY_COMMIT]
    inventory["all"]["vars"]["customer_cluster_ingress_vip"] = vip
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "source_mismatch"


async def test_floating_ip_must_match_current_operator_status(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
) -> None:
    managed_source.documents[source_cluster.slug]["status"]["ingressFloatingIp"] = "192.0.2.41"
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize("field,value", [
    ("inventoryPath", "clusters/other/generated/ansible/hosts.yml"),
    ("inventoryPath", "../secrets.yml"), ("inventoryPath", None),
    ("inventoryCommit", None), ("inventoryCommit", 123),
])
async def test_inventory_identity_is_validated_before_reading_git(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    field: str, value: Any,
) -> None:
    managed_source.documents[source_cluster.slug]["status"][field] = value
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "invalid_source" and not managed_source.reads


@pytest.mark.parametrize("revision", ["main", "HEAD", "a" * 39, "b" * 63, "--all", ""])
async def test_source_cannot_use_a_branch_or_abbreviated_object_id(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    revision: str,
) -> None:
    managed_source.documents[source_cluster.slug]["status"]["inventoryCommit"] = revision
    backend = ClusterGitBackend(source_settings)
    backend.repo = Mock()
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, backend, source_settings)
    assert caught.value.code == "inventory_unavailable"
    assert not backend.repo.mock_calls


async def test_missing_exact_inventory_never_falls_back_to_newer_head(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
) -> None:
    managed_source.documents[source_cluster.slug]["status"]["inventoryCommit"] = "f" * 40
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "inventory_unavailable"
    assert managed_source.reads == [(source_cluster.slug, "f" * 40)]


async def test_missing_management_configuration_is_actionable(
    source_cluster: TenantCluster, source_settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, None, source_settings)
    assert caught.value.code == "source_unavailable"
    monkeypatch.setattr(k8s, "_api", None)
    assert (await gitops_source.observe(source_cluster.slug, source_settings))["status"] == (
        "unavailable"
    )
    assert (await gitops_source.observe(
        source_cluster.slug, replace(source_settings, managed_cluster_namespace="")
    ))["status"] == "unconfigured"


@pytest.mark.parametrize("section,changes,expected", [
    ("metadata", {"deletionTimestamp": "2026-09-15T17:00:00Z"}, "deleting"),
    ("spec", {"suspend": True}, "suspended"),
    ("status", {"phase": "Suspended"}, "suspended"),
    ("status", {"phase": "Failed"}, "failed"),
])
async def test_explicit_operator_states_override_a_leftover_ready_condition(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    section: str, changes: dict[str, Any], expected: str,
) -> None:
    document = managed_source.documents[source_cluster.slug]
    document[section].update(changes)
    assert document["status"]["conditions"][0]["status"] == "True"
    observation = await gitops_source.observe(source_cluster.slug, source_settings)
    assert observation["status"] == expected
    assert observation["message"] != "Infrastructure and inventory verified"
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "inventory_pending" and not managed_source.reads


async def test_boolean_observed_generation_does_not_equal_integer_generation_one(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
) -> None:
    document = managed_source.documents[source_cluster.slug]
    document["metadata"]["generation"] = 1
    document["status"]["observedGeneration"] = True
    assert (await gitops_source.observe(source_cluster.slug, source_settings))["status"] == (
        "pending"
    )
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "inventory_pending" and not managed_source.reads


@pytest.mark.parametrize("spec", ["malformed-spec", ["not-a-mapping"], 7])
async def test_malformed_raw_spec_is_a_categorized_error_not_an_attribute_error(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    spec: Any,
) -> None:
    managed_source.documents[source_cluster.slug]["spec"] = spec
    observed = await gitops_source.observe(source_cluster.slug, source_settings)
    assert observed["status"] == "invalid_source"
    with pytest.raises(CustomerGitOpsError) as caught:
        await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert caught.value.code == "invalid_source" and not managed_source.reads


async def test_pre_push_gate_accepts_unchanged_snapshot_with_a_fresh_kubernetes_read(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
) -> None:
    snapshot = await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    assert len(managed_source.api_calls) == len(managed_source.reads) == 1
    gitops_source.assert_fresh(snapshot, source_settings)
    assert len(managed_source.api_calls) == 2
    assert managed_source.reads == [(source_cluster.slug, INVENTORY_COMMIT)]


@pytest.mark.parametrize("section,changes", [
    ("metadata", {"uid": "replacement-cluster"}),
    ("metadata", {"generation": 8}),
    ("metadata", {"deletionTimestamp": "2026-09-15T17:00:00Z"}),
    ("spec", {"suspend": True}),
    ("spec", {"dns": {"argocdHostname": "changed.operator.test"}}),
    ("status", {"phase": "Suspended"}),
    ("status", {"phase": "Failed"}),
    ("status", {"inventoryCommit": "b" * 40}),
    ("status", {"inventoryPath": "clusters/another/generated/ansible/hosts.yml"}),
    ("status", {"observedGeneration": 6}),
    ("status", {"conditions": [{"type": "Ready", "status": "False"}]}),
])
async def test_pre_push_gate_rejects_source_changes_since_snapshot_validation(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    section: str, changes: dict[str, Any],
) -> None:
    snapshot = await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    managed_source.documents[source_cluster.slug][section].update(changes)
    with pytest.raises(CustomerGitOpsError) as caught:
        gitops_source.assert_fresh(snapshot, source_settings)
    assert caught.value.code == "source_changed"
    assert len(managed_source.api_calls) == 2 and len(managed_source.reads) == 1


@pytest.mark.parametrize("changes", [
    {"managed_cluster_namespace": "customer-clusters-prod"},
    {"customer_cluster_node_interface": "ens9"},
])
async def test_pre_push_gate_rejects_changed_deployment_namespace_or_interface(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    changes: dict[str, str],
) -> None:
    snapshot = await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    with pytest.raises(CustomerGitOpsError) as caught:
        gitops_source.assert_fresh(snapshot, replace(source_settings, **changes))
    assert caught.value.code == "source_changed"


@pytest.mark.parametrize("status", [403, 404, 503])
async def test_pre_push_source_lookup_failure_is_sanitized_and_blocks_publication(
    managed_source: ManagedSource, source_cluster: TenantCluster, source_settings: Settings,
    status: int,
) -> None:
    snapshot = await gitops_source.load_snapshot(source_cluster, managed_source, source_settings)
    error = ApiException(status=status, reason=SOURCE_SECRET)
    error.body = SOURCE_SECRET
    managed_source.error = error
    with pytest.raises(CustomerGitOpsError) as caught:
        gitops_source.assert_fresh(snapshot, source_settings)
    assert caught.value.code == "source_changed" and SOURCE_SECRET not in str(caught.value)
