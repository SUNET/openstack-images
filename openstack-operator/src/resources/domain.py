"""Domain resource management for OpenStack operator."""

import logging
from typing import Any

from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)


def ensure_domain(
    client: OpenStackClient,
    name: str,
    description: str = "",
    enabled: bool = True,
) -> str:
    """Ensure a domain exists with the given configuration.

    Creates the domain if it doesn't exist, or returns the existing one.

    Args:
        client: OpenStack client
        name: Domain name
        description: Domain description
        enabled: Whether the domain is enabled

    Returns:
        The domain ID
    """
    existing = client.get_domain(name)

    if existing:
        logger.info(f"Domain {name} already exists (id={existing.id})")
        # Update if needed
        client.update_domain(existing.id, description=description, enabled=enabled)
        return existing.id

    # Create new domain
    domain = client.create_domain(name, description=description, enabled=enabled)
    logger.info(f"Created domain {name} (id={domain.id})")
    return domain.id


def delete_domain(client: OpenStackClient, domain_id: str) -> None:
    """Delete a domain.

    Note: Domain must be disabled before deletion, which the client handles.

    Args:
        client: OpenStack client
        domain_id: The domain ID to delete
    """
    client.delete_domain(domain_id)


def get_domain_info(client: OpenStackClient, name: str) -> dict[str, Any] | None:
    """Get domain information by name.

    Args:
        client: OpenStack client
        name: Domain name

    Returns:
        Dict with domain_id and other info, or None if not found
    """
    domain = client.get_domain(name)
    if not domain:
        return None

    return {
        "domain_id": domain.id,
        "name": domain.name,
        "description": domain.description,
        "enabled": domain.is_enabled,
    }
