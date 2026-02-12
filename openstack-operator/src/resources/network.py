"""Network, subnet, and router management."""

import logging
from typing import Any

from constants import MANAGED_BY_TAG
from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)

# Tags to apply to all created resources
_RESOURCE_TAGS = [MANAGED_BY_TAG]


def ensure_network(
    client: OpenStackClient,
    project_id: str,
    network_spec: dict[str, Any],
) -> dict[str, str]:
    """Ensure a network, subnet, and optionally router exist.

    Args:
        client: OpenStack client
        project_id: Project ID to create resources in
        network_spec: Network specification from CR

    Returns:
        Dict with networkId, subnetId, and optionally routerId
    """
    name = network_spec["name"]
    cidr = network_spec["cidr"]
    enable_dhcp = network_spec.get("enableDhcp", True)
    dns_nameservers = network_spec.get("dnsNameservers", [])
    router_spec = network_spec.get("router")

    result: dict[str, str] = {"name": name}

    # Create or get network
    network = client.get_network(name, project_id)
    if network:
        logger.info(f"Network {name} already exists with ID {network.id}")
        result["networkId"] = network.id
    else:
        network = client.create_network(name, project_id, tags=_RESOURCE_TAGS)
        result["networkId"] = network.id
        logger.info(f"Created network {name} with ID {network.id}")

    # Create or get subnet
    subnet_name = f"{name}-subnet"
    subnet = client.get_subnet(subnet_name, network.id)
    if subnet:
        logger.info(f"Subnet {subnet_name} already exists with ID {subnet.id}")
        result["subnetId"] = subnet.id
    else:
        subnet = client.create_subnet(
            subnet_name,
            network.id,
            cidr,
            enable_dhcp=enable_dhcp,
            dns_nameservers=dns_nameservers,
            tags=_RESOURCE_TAGS,
        )
        result["subnetId"] = subnet.id
        logger.info(f"Created subnet {subnet_name} with ID {subnet.id}")

    # Create router if specified
    if router_spec:
        router_name = f"{name}-router"
        router = client.get_router(router_name, project_id)

        external_network_name = router_spec.get("externalNetwork")
        enable_snat = router_spec.get("enableSnat", True)
        external_network_id = None

        if external_network_name:
            ext_network = client.get_external_network(external_network_name)
            if ext_network:
                external_network_id = ext_network.id
            else:
                logger.warning(
                    f"External network {external_network_name} not found, "
                    "router will not have external gateway"
                )

        if router:
            logger.info(f"Router {router_name} already exists with ID {router.id}")
            result["routerId"] = router.id
        else:
            router = client.create_router(
                router_name,
                project_id,
                external_network_id=external_network_id,
                enable_snat=enable_snat,
                tags=_RESOURCE_TAGS,
            )
            result["routerId"] = router.id
            logger.info(f"Created router {router_name} with ID {router.id}")

        # Add interface to router
        client.add_router_interface(router.id, subnet.id)

    return result


def delete_network(
    client: OpenStackClient,
    network_status: dict[str, str],
) -> None:
    """Delete a network and its associated resources.

    Args:
        client: OpenStack client
        network_status: Status dict with networkId, subnetId, routerId
    """
    router_id = network_status.get("routerId")
    subnet_id = network_status.get("subnetId")
    network_id = network_status.get("networkId")

    # Remove router interface and delete router first
    if router_id and subnet_id:
        try:
            client.remove_router_interface(router_id, subnet_id)
        except Exception as e:
            logger.warning(f"Failed to remove router interface: {e}")

    if router_id:
        try:
            client.delete_router(router_id)
        except Exception as e:
            logger.warning(f"Failed to delete router {router_id}: {e}")

    # Delete subnet
    if subnet_id:
        try:
            client.delete_subnet(subnet_id)
        except Exception as e:
            logger.warning(f"Failed to delete subnet {subnet_id}: {e}")

    # Delete network
    if network_id:
        try:
            client.delete_network(network_id)
        except Exception as e:
            logger.warning(f"Failed to delete network {network_id}: {e}")


def ensure_networks(
    client: OpenStackClient,
    project_id: str,
    network_specs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Ensure all specified networks exist.

    Returns:
        List of network status dicts
    """
    results = []
    for spec in network_specs:
        result = ensure_network(client, project_id, spec)
        results.append(result)
    return results


def delete_networks(
    client: OpenStackClient,
    network_statuses: list[dict[str, str]],
) -> None:
    """Delete all networks from status."""
    for status in network_statuses:
        delete_network(client, status)
