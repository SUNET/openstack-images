"""Garbage collection daemon for cluster-scoped resources.

This module handles cleanup of orphaned OpenStack resources that were
created by the operator but no longer have corresponding Kubernetes CRs.
"""

import logging
import os
import time
from typing import Any

import kopf
from kubernetes import client as k8s_client

from openstack_client import OpenStackClient
from resources.registry import ResourceRegistry
from state import state, get_openstack_client, get_registry
from metrics import CLUSTER_GC_RUNS, CLUSTER_GC_DELETED_RESOURCES, CLUSTER_GC_DURATION

logger = logging.getLogger(__name__)


def _get_expected_cr_names(api: k8s_client.CustomObjectsApi, plural: str) -> set[str]:
    """Get set of CR names for a given resource type."""
    try:
        crs = api.list_cluster_custom_object(
            group="sunet.se",
            version="v1alpha1",
            plural=plural,
        )
        return {
            item.get("metadata", {}).get("name", "")
            for item in crs.get("items", [])
        }
    except k8s_client.ApiException as e:
        if e.status == 404:
            # CRD doesn't exist yet
            return set()
        raise


def _collect_cluster_garbage(
    client: OpenStackClient,
    registry: ResourceRegistry,
    expected_crs: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Remove orphaned cluster-scoped resources using registry.

    Args:
        client: OpenStack client
        registry: Resource registry
        expected_crs: Dict mapping resource type to set of expected CR names

    Returns:
        Dict with lists of deleted resource names by type
    """
    result: dict[str, list[str]] = {}

    # Define resource types and their delete functions
    # Order matters: delete dependent resources first
    resource_configs = [
        ("provider_networks", _delete_provider_network),
        ("images", _delete_image),
        ("flavors", _delete_flavor),
        ("domains", _delete_domain),
    ]

    for resource_type, delete_fn in resource_configs:
        result[f"deleted_{resource_type}"] = []
        orphans = registry.get_orphans(
            resource_type, expected_crs.get(resource_type, set())
        )

        for orphan in orphans:
            try:
                delete_fn(client, orphan)
                registry.unregister(resource_type, orphan["name"])
                result[f"deleted_{resource_type}"].append(orphan["name"])
                logger.info(
                    f"Deleted orphaned {resource_type[:-1]}: {orphan['name']}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to delete orphaned {resource_type[:-1]} "
                    f"{orphan['name']}: {e}"
                )

    return result


def _delete_domain(client: OpenStackClient, orphan: dict[str, Any]) -> None:
    """Delete an orphaned domain."""
    client.delete_domain(orphan["id"])


def _delete_flavor(client: OpenStackClient, orphan: dict[str, Any]) -> None:
    """Delete an orphaned flavor."""
    client.delete_flavor(orphan["id"])


def _delete_image(client: OpenStackClient, orphan: dict[str, Any]) -> None:
    """Delete an orphaned image."""
    client.delete_image(orphan["id"])


def _delete_provider_network(client: OpenStackClient, orphan: dict[str, Any]) -> None:
    """Delete an orphaned provider network and its subnets."""
    # Delete subnets first
    for subnet_id in orphan.get("subnets", []):
        try:
            client.delete_subnet(subnet_id)
        except Exception as e:
            logger.warning(f"Failed to delete subnet {subnet_id}: {e}")

    # Then delete network
    client.delete_network(orphan["id"])


@kopf.daemon("sunet.se", "v1alpha1", "openstackdomains", cancellation_timeout=10)
async def cluster_garbage_collector(
    name: str,
    stopped: kopf.DaemonStopped,
    **_: Any,
) -> None:
    """Periodic garbage collection daemon for cluster-scoped resources.

    Runs every 10 minutes to clean up orphaned resources in OpenStack
    that don't have corresponding CRs.

    This daemon attaches to OpenstackDomain CRs. Only the "leader"
    (first CR alphabetically) actually runs GC to avoid duplicate work.
    """
    gc_interval = int(os.environ.get("CLUSTER_GC_INTERVAL_SECONDS", "600"))
    my_identity = name

    # Ensure k8s config is loaded
    state.get_k8s_custom_api()

    while not stopped:
        try:
            api = state.get_k8s_custom_api()

            # Simple leader election: only the first domain CR runs GC
            domain_crs = api.list_cluster_custom_object(
                group="sunet.se",
                version="v1alpha1",
                plural="openstackdomains",
            )

            cr_items = domain_crs.get("items", [])
            if not cr_items:
                logger.debug("No OpenstackDomain CRs found, skipping cluster GC")
                await stopped.wait(gc_interval)
                continue

            sorted_crs = sorted(
                cr_items,
                key=lambda x: x.get("metadata", {}).get("name", ""),
            )
            leader_name = sorted_crs[0].get("metadata", {}).get("name", "")

            if my_identity != leader_name:
                logger.debug(
                    f"Cluster GC skipped on {my_identity}, leader is {leader_name}"
                )
                await stopped.wait(gc_interval)
                continue

            logger.info("Running cluster-scoped garbage collection")
            gc_start_time = time.monotonic()

            # Get expected CRs for each resource type
            expected_crs = {
                "domains": _get_expected_cr_names(api, "openstackdomains"),
                "flavors": _get_expected_cr_names(api, "openstackflavors"),
                "images": _get_expected_cr_names(api, "openstackimages"),
                "provider_networks": _get_expected_cr_names(api, "openstacknetworks"),
            }

            # Run GC
            client = get_openstack_client()
            registry = get_registry()
            result = _collect_cluster_garbage(client, registry, expected_crs)

            gc_duration = time.monotonic() - gc_start_time
            CLUSTER_GC_DURATION.observe(gc_duration)

            # Log and record metrics
            total_deleted = sum(len(v) for v in result.values())
            if total_deleted > 0:
                for resource_type, deleted_list in result.items():
                    if deleted_list:
                        # Extract resource type from key (e.g., "deleted_domains" -> "domain")
                        rtype = resource_type.replace("deleted_", "").rstrip("s")
                        CLUSTER_GC_DELETED_RESOURCES.labels(resource_type=rtype).inc(len(deleted_list))
                logger.info(f"Cluster GC completed: {result}")
            else:
                logger.debug("Cluster GC completed: no orphaned resources found")

            CLUSTER_GC_RUNS.labels(status="success").inc()

        except Exception as e:
            logger.error(f"Cluster garbage collection failed: {e}")
            CLUSTER_GC_RUNS.labels(status="error").inc()

        await stopped.wait(gc_interval)
