"""Garbage collection for orphaned OpenStack resources.

This module handles cleanup of orphaned project-scoped resources.
For cluster-scoped resources (domains, flavors, images, provider networks),
see handlers/gc_cluster.py.
"""

import logging
from typing import Any

from constants import MANAGED_BY_TAG
from openstack_client import OpenStackClient
from resources.federation import FederationManager
from resources.registry import ResourceRegistry
from utils import make_group_name

logger = logging.getLogger(__name__)


def collect_garbage(
    client: OpenStackClient,
    managed_domain: str,
    expected_projects: set[str],
    federation_config: dict[str, str] | None = None,
    registry: ResourceRegistry | None = None,
) -> dict[str, list[str]]:
    """Remove orphaned projects, groups, and federation mappings.

    Uses both tag-based detection (legacy) and registry-based detection.
    Resources tracked in the registry are preferred for identifying orphans.

    Args:
        client: OpenStack client
        managed_domain: Domain name to scan for orphans (e.g., 'sso-users')
        expected_projects: Set of project names that should exist (from CRs)
        federation_config: Optional dict with idp_name, idp_remote_id, sso_domain
        registry: Optional resource registry for ConfigMap-based tracking

    Returns:
        Dict with 'deleted_projects', 'deleted_groups', 'deleted_mappings' lists
    """
    result: dict[str, list[str]] = {
        "deleted_projects": [],
        "deleted_groups": [],
        "deleted_mappings": [],
    }

    domain = client.get_domain(managed_domain)
    if not domain:
        logger.warning(f"Domain {managed_domain} not found, skipping GC")
        return result

    orphaned_projects: list[str] = []

    # Method 1: Registry-based orphan detection (preferred)
    if registry:
        orphans = registry.get_orphans("projects", expected_projects)
        for orphan in orphans:
            project_name = orphan["name"]
            logger.info(
                f"Found orphaned project (registry): {project_name} in domain {managed_domain}"
            )
            orphaned_projects.append(project_name)

            # Delete associated group
            group_orphans = registry.get_by_cr("groups", orphan.get("cr_name", ""))
            for group_orphan in group_orphans:
                try:
                    client.delete_group(group_orphan["id"])
                    registry.unregister("groups", group_orphan["name"])
                    result["deleted_groups"].append(group_orphan["name"])
                    logger.info(f"Deleted orphaned group {group_orphan['name']}")
                except Exception as e:
                    logger.error(f"Failed to delete group {group_orphan['name']}: {e}")

            # Delete the project
            try:
                client.delete_project(orphan["id"])
                registry.unregister("projects", project_name)
                result["deleted_projects"].append(project_name)
                logger.info(f"Deleted orphaned project {project_name}")
            except Exception as e:
                logger.error(f"Failed to delete project {project_name}: {e}")

    # Method 2: Tag-based orphan detection (legacy/fallback)
    # This catches projects that were created before registry was introduced
    projects = client.list_projects_with_tag(domain.id, MANAGED_BY_TAG)
    logger.debug(f"Found {len(projects)} tagged projects in {managed_domain}")

    for project in projects:
        # Skip if already handled via registry
        if project.name in orphaned_projects:
            continue
        # Skip if project should exist
        if project.name in expected_projects:
            continue

        logger.info(
            f"Found orphaned project (tag): {project.name} in domain {managed_domain}"
        )
        orphaned_projects.append(project.name)

        # Find and delete associated group
        group_name = make_group_name(project.name)
        group = client.get_group(group_name, managed_domain)
        if group:
            try:
                client.delete_group(group.id)
                result["deleted_groups"].append(group_name)
                logger.info(f"Deleted orphaned group {group_name}")
            except Exception as e:
                logger.error(f"Failed to delete group {group_name}: {e}")

        # Delete the project
        try:
            client.delete_project(project.id)
            result["deleted_projects"].append(project.name)
            logger.info(f"Deleted orphaned project {project.name}")
        except Exception as e:
            logger.error(f"Failed to delete project {project.name}: {e}")

    # Clean up federation mappings for orphaned projects
    if federation_config and orphaned_projects:
        _cleanup_federation_mappings(
            client, federation_config, orphaned_projects, result, registry
        )

    return result


def _cleanup_federation_mappings(
    client: OpenStackClient,
    federation_config: dict[str, str],
    orphaned_projects: list[str],
    result: dict[str, list[str]],
    registry: ResourceRegistry | None = None,
) -> None:
    """Remove federation mapping rules for orphaned projects."""
    try:
        manager = FederationManager(
            client,
            federation_config["idp_name"],
            federation_config["idp_remote_id"],
            federation_config["sso_domain"],
        )

        for project_name in orphaned_projects:
            try:
                manager.remove_project_mapping(project_name)
                if registry:
                    registry.unregister("federation_mappings", project_name)
                result["deleted_mappings"].append(project_name)
                logger.info(f"Removed federation mapping for orphaned project {project_name}")
            except Exception as e:
                logger.error(
                    f"Failed to remove federation mapping for {project_name}: {e}"
                )
    except Exception as e:
        logger.error(f"Failed to initialize FederationManager for GC: {e}")


def get_expected_projects_from_crs(
    cr_list: list[dict[str, Any]],
) -> set[str]:
    """Extract expected project names from OpenstackProject CRs.

    Args:
        cr_list: List of OpenstackProject CR dicts

    Returns:
        Set of project names that should exist
    """
    expected: set[str] = set()
    for cr in cr_list:
        spec = cr.get("spec", {})
        project_name = spec.get("name")
        if project_name:
            expected.add(project_name)
    return expected


def get_federation_config_from_crs(
    cr_list: list[dict[str, Any]],
    k8s_core_api: Any,
) -> dict[str, str] | None:
    """Extract federation config from the first CR that has one.

    Args:
        cr_list: List of OpenstackProject CR dicts
        k8s_core_api: Kubernetes CoreV1Api instance

    Returns:
        Dict with idp_name, idp_remote_id, sso_domain or None
    """
    for cr in cr_list:
        spec = cr.get("spec", {})
        federation_ref = spec.get("federationRef")
        if federation_ref:
            config_map_name = federation_ref.get("configMapName")
            config_map_ns = federation_ref.get("configMapNamespace")
            if config_map_name and config_map_ns:
                try:
                    cm = k8s_core_api.read_namespaced_config_map(
                        config_map_name, config_map_ns
                    )
                    data = cm.data or {}
                    if data.get("IDP_NAME"):
                        return {
                            "idp_name": data.get("IDP_NAME", ""),
                            "idp_remote_id": data.get("IDP_REMOTE_ID", ""),
                            "sso_domain": data.get("SSO_DOMAIN", ""),
                        }
                except Exception as e:
                    logger.debug(f"Could not read federation config: {e}")
                    continue
    return None
