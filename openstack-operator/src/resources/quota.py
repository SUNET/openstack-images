"""Quota management for OpenStack projects."""

import logging
from typing import Any

from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)


def apply_quotas(
    client: OpenStackClient, project_id: str, quotas: dict[str, Any]
) -> None:
    """Apply quotas to a project.

    Args:
        client: OpenStack client
        project_id: Project ID to set quotas for
        quotas: Quota specification with 'compute', 'storage', 'network' sections
    """
    if not quotas:
        logger.debug(f"No quotas specified for project {project_id}")
        return

    compute_quotas = quotas.get("compute", {})
    if compute_quotas:
        logger.info(f"Setting compute quotas for {project_id}: {compute_quotas}")
        client.set_compute_quotas(project_id, compute_quotas)

    storage_quotas = quotas.get("storage", {})
    if storage_quotas:
        logger.info(f"Setting storage quotas for {project_id}: {storage_quotas}")
        client.set_volume_quotas(project_id, storage_quotas)

    network_quotas = quotas.get("network", {})
    if network_quotas:
        logger.info(f"Setting network quotas for {project_id}: {network_quotas}")
        client.set_network_quotas(project_id, network_quotas)
