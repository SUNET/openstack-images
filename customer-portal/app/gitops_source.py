"""Allowlisted, immutable inputs for customer GitOps generation."""

import asyncio
from dataclasses import asdict, dataclass
from ipaddress import IPv4Address
from typing import Any

from app.cluster_git_backend import ClusterGitBackend
from app.config import Settings
from app.customer_gitops import CustomerGitOpsError
from app.k8s import ManagedClusterLookupError, get_managed_cluster
from app.models import TenantCluster


@dataclass(frozen=True)
class SourceSnapshot:
    namespace: str
    slug: str
    uid: str
    generation: int
    inventory_commit: str
    inventory_path: str
    hostname: str
    ingress_vip: str
    interface: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def readiness(document: dict, slug: str, namespace: str) -> dict[str, Any]:
    """Report pending verification separately from missing or malformed objects."""
    metadata = document.get("metadata") or {}
    status = document.get("status") or {}
    spec = document.get("spec", {})
    if not all(isinstance(part, dict) for part in (metadata, spec, status)):
        raise CustomerGitOpsError("ManagedCluster has an invalid shape", "invalid_source")
    if metadata.get("name") != slug or metadata.get("namespace") != namespace:
        raise CustomerGitOpsError("ManagedCluster identity does not match", "invalid_source")
    generation = metadata.get("generation")
    conditions = status.get("conditions") or []
    if not isinstance(conditions, list):
        raise CustomerGitOpsError("ManagedCluster conditions are invalid", "invalid_source")
    condition = next(
        (c for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"), {}
    )
    phase = status.get("phase")
    deleting = bool(metadata.get("deletionTimestamp"))
    suspended = spec.get("suspend") is True or phase == "Suspended"
    ready = (
        type(generation) is int and generation > 0
        and type(status.get("observedGeneration")) is int
        and status.get("observedGeneration") == generation
        and phase == "VirtualMachinesReady" and not deleting and not suspended
        and condition.get("status") == "True"
    )
    result = "ready" if ready else "pending"
    message = "Infrastructure and inventory verified" if ready else (
        "Waiting for the operator to verify this ManagedCluster generation"
    )
    if deleting:
        result, message = "deleting", "ManagedCluster is being deleted; publication is blocked"
    elif suspended:
        result = "suspended"
        message = "Provisioning is suspended; review the management declaration"
    elif phase == "Failed":
        result, message = "failed", "Operator verification failed; inspect the reported reason"
    return {
        "status": result,
        "namespace": namespace,
        "name": slug,
        "phase": status.get("phase"),
        "reason": condition.get("reason"),
        "message": message,
        "inventory_path": status.get("inventoryPath"),
        "inventory_commit": status.get("inventoryCommit"),
    }


def assert_fresh(snapshot: SourceSnapshot, settings: Settings) -> None:
    """Last read-only check inside the publisher immediately before a new push."""
    try:
        document = get_managed_cluster(snapshot.slug, snapshot.namespace)
        observation = readiness(document, snapshot.slug, snapshot.namespace)
        metadata = document["metadata"]
        if (
            observation["status"] != "ready"
            or metadata.get("uid") != snapshot.uid
            or metadata.get("generation") != snapshot.generation
            or observation["inventory_commit"] != snapshot.inventory_commit
            or observation["inventory_path"] != snapshot.inventory_path
            or document["spec"]["dns"]["argocdHostname"] != snapshot.hostname
            or settings.managed_cluster_namespace != snapshot.namespace
            or settings.customer_cluster_node_interface != snapshot.interface
        ):
            raise ValueError()
    except (ManagedClusterLookupError, CustomerGitOpsError, KeyError, TypeError, ValueError):
        raise CustomerGitOpsError(
            "Infrastructure changed during validation; refresh status and preview again",
            "source_changed",
        ) from None


async def observe(slug: str, settings: Settings) -> dict[str, Any]:
    try:
        document = await asyncio.to_thread(
            get_managed_cluster, slug, settings.managed_cluster_namespace
        )
        return readiness(document, slug, settings.managed_cluster_namespace)
    except (ManagedClusterLookupError, CustomerGitOpsError) as exc:
        return {
            "status": exc.code or "invalid_source",
            "namespace": settings.managed_cluster_namespace,
            "name": slug,
            "phase": None,
            "reason": exc.code,
            "message": str(exc),
            "inventory_path": None,
            "inventory_commit": None,
        }


async def load_snapshot(
    cluster: TenantCluster, backend: ClusterGitBackend | None, settings: Settings
) -> SourceSnapshot:
    if backend is None:
        raise CustomerGitOpsError("Management repository is not configured", "source_unavailable")
    try:
        document = await asyncio.to_thread(
            get_managed_cluster, cluster.slug, settings.managed_cluster_namespace
        )
    except ManagedClusterLookupError as exc:
        raise CustomerGitOpsError(str(exc), exc.code) from None
    observation = readiness(document, cluster.slug, settings.managed_cluster_namespace)
    if observation["status"] != "ready":
        raise CustomerGitOpsError(observation["message"], "inventory_pending")
    metadata, spec, status = document["metadata"], document.get("spec"), document["status"]
    path = f"clusters/{cluster.slug}/generated/ansible/hosts.yml"
    if observation["inventory_path"] != path or not isinstance(spec, dict):
        raise CustomerGitOpsError("Inventory path or declaration is invalid", "invalid_source")
    commit = observation["inventory_commit"]
    if not isinstance(commit, str) or not isinstance(metadata.get("uid"), str):
        raise CustomerGitOpsError("Inventory commit or cluster UID is missing", "invalid_source")
    try:
        declaration, inventory = await asyncio.to_thread(
            backend.read_cluster_snapshot, cluster.slug, commit
        )
    except (ValueError, TypeError):
        raise CustomerGitOpsError(
            "The operator-reported inventory commit cannot be read; refresh operator status",
            "inventory_unavailable",
        ) from None
    try:
        historical_spec = declaration["spec"]
        if (
            declaration["kind"] != "ManagedCluster"
            or declaration["metadata"]["name"] != cluster.slug
            or spec["contractNumber"] != cluster.contract.contract_number
            or spec["customerDomain"] != cluster.contract.customer.domain
            or spec["openstack"]["projectResourceName"]
            != cluster.management_project_resource_name
        ):
            raise ValueError()
        for key in ("contractNumber", "customerDomain", "openstack", "workerGroups", "profileRef"):
            if historical_spec.get(key) != spec.get(key):
                raise ValueError()
        hostname = spec["dns"]["argocdHostname"]
        if hostname != historical_spec["dns"]["argocdHostname"]:
            raise ValueError()
        variables = inventory["all"]["vars"]
        vip = IPv4Address(variables["customer_cluster_ingress_vip"])
        public_ip = IPv4Address(variables["customer_cluster_ingress_floating_ip"])
        if (
            not vip.is_private or vip.is_loopback or vip.is_link_local
            or vip.is_unspecified or vip.is_multicast
            or vip == public_ip or str(public_ip) != status["ingressFloatingIp"]
            or str(vip) == variables["customer_cluster_api_vip"]
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise CustomerGitOpsError(
            "Inventory and declaration do not match the current cluster allocation",
            "source_mismatch",
        ) from None
    return SourceSnapshot(
        namespace=settings.managed_cluster_namespace,
        slug=cluster.slug,
        uid=metadata["uid"],
        generation=metadata["generation"],
        inventory_commit=commit,
        inventory_path=path,
        hostname=hostname,
        ingress_vip=str(vip),
        interface=settings.customer_cluster_node_interface,
    )
