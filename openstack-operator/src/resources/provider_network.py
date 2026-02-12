"""Provider network resource management for OpenStack operator."""

import logging
from typing import Any

from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)


def ensure_provider_network(
    client: OpenStackClient,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Ensure a provider network exists with the given configuration.

    Creates the network and subnets if they don't exist.

    Args:
        client: OpenStack client
        spec: Network specification from CRD

    Returns:
        Dict with networkId and subnets list
    """
    name = spec["name"]
    existing = client.get_network_by_name(name)

    if existing:
        logger.info(f"Provider network {name} already exists (id={existing.id})")
        # Get existing subnets
        subnets_status = _get_subnet_statuses(client, existing.id)
        return {
            "networkId": existing.id,
            "subnets": subnets_status,
        }

    # Create new network
    network = client.create_provider_network(
        name=name,
        network_type=spec.get("providerNetworkType", "flat"),
        physical_network=spec.get("providerPhysicalNetwork"),
        segmentation_id=spec.get("providerSegmentationId"),
        external=spec.get("external", False),
        shared=spec.get("shared", False),
        description=spec.get("description", ""),
    )
    logger.info(f"Created provider network {name} (id={network.id})")

    # Create subnets
    subnets_status = []
    for subnet_spec in spec.get("subnets", []):
        subnet_status = _ensure_subnet(client, network.id, subnet_spec)
        subnets_status.append(subnet_status)

    return {
        "networkId": network.id,
        "subnets": subnets_status,
    }


def _ensure_subnet(
    client: OpenStackClient,
    network_id: str,
    spec: dict[str, Any],
) -> dict[str, str]:
    """Ensure a subnet exists on the network.

    Args:
        client: OpenStack client
        network_id: Parent network ID
        spec: Subnet specification

    Returns:
        Dict with name and subnetId
    """
    name = spec["name"]
    cidr = spec["cidr"]

    # Check if subnet already exists
    existing = client.get_subnet(name, network_id)
    if existing:
        logger.info(f"Subnet {name} already exists (id={existing.id})")
        return {"name": name, "subnetId": existing.id}

    # Build allocation pools
    allocation_pools = None
    if "allocationPools" in spec:
        allocation_pools = [
            {"start": pool["start"], "end": pool["end"]}
            for pool in spec["allocationPools"]
        ]

    # Create subnet
    subnet = client.create_subnet_with_pools(
        name=name,
        network_id=network_id,
        cidr=cidr,
        gateway_ip=spec.get("gatewayIp"),
        enable_dhcp=spec.get("enableDhcp", True),
        dns_nameservers=spec.get("dnsNameservers"),
        allocation_pools=allocation_pools,
    )
    logger.info(f"Created subnet {name} (id={subnet.id})")

    return {"name": name, "subnetId": subnet.id}


def _get_subnet_statuses(
    client: OpenStackClient,
    network_id: str,
) -> list[dict[str, str]]:
    """Get status of all subnets on a network.

    Args:
        client: OpenStack client
        network_id: Network ID

    Returns:
        List of dicts with name and subnetId
    """
    subnets = client.list_subnets(network_id)
    return [{"name": s.name, "subnetId": s.id} for s in subnets]


def delete_provider_network(
    client: OpenStackClient,
    network_id: str,
    subnet_ids: list[str] | None = None,
) -> None:
    """Delete a provider network and its subnets.

    Args:
        client: OpenStack client
        network_id: The network ID to delete
        subnet_ids: Optional list of subnet IDs to delete first
    """
    # Delete subnets first
    if subnet_ids:
        for subnet_id in subnet_ids:
            try:
                client.delete_subnet(subnet_id)
            except Exception as e:
                logger.warning(f"Failed to delete subnet {subnet_id}: {e}")

    # Delete network
    client.delete_network(network_id)


def get_provider_network_info(
    client: OpenStackClient,
    name: str,
) -> dict[str, Any] | None:
    """Get provider network information by name.

    Args:
        client: OpenStack client
        name: Network name

    Returns:
        Dict with network_id and subnets, or None if not found
    """
    network = client.get_network_by_name(name)
    if not network:
        return None

    subnets = _get_subnet_statuses(client, network.id)

    return {
        "network_id": network.id,
        "name": network.name,
        "subnets": subnets,
    }
