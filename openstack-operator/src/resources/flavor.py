"""Flavor resource management for OpenStack operator."""

import logging
from typing import Any

import kopf

from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)

# Immutable flavor properties that require recreation if changed
IMMUTABLE_FLAVOR_FIELDS = {"vcpus", "ram", "disk", "ephemeral", "swap", "isPublic"}


def ensure_flavor(client: OpenStackClient, spec: dict[str, Any]) -> str:
    """Ensure a flavor exists with the given configuration.

    Creates the flavor if it doesn't exist. Flavors are immutable in OpenStack,
    so updates to core properties require delete and recreate (handled by caller).

    Args:
        client: OpenStack client
        spec: Flavor specification from CRD

    Returns:
        The flavor ID
    """
    name = spec["name"]
    existing = client.get_flavor(name)

    if existing:
        logger.info(f"Flavor {name} already exists (id={existing.id})")
        # Update extra specs if provided
        extra_specs = spec.get("extraSpecs", {})
        if extra_specs:
            client.set_flavor_extra_specs(existing.id, extra_specs)
        return existing.id

    # Create new flavor
    flavor = client.create_flavor(
        name=name,
        vcpus=spec["vcpus"],
        ram=spec["ram"],
        disk=spec.get("disk", 0),
        ephemeral=spec.get("ephemeral", 0),
        swap=spec.get("swap", 0),
        is_public=spec.get("isPublic", True),
        description=spec.get("description", ""),
    )
    logger.info(f"Created flavor {name} (id={flavor.id})")

    # Set extra specs if provided
    extra_specs = spec.get("extraSpecs", {})
    if extra_specs:
        client.set_flavor_extra_specs(flavor.id, extra_specs)

    return flavor.id


def delete_flavor(client: OpenStackClient, flavor_id: str) -> None:
    """Delete a flavor.

    Args:
        client: OpenStack client
        flavor_id: The flavor ID to delete
    """
    client.delete_flavor(flavor_id)


def flavor_needs_recreate(diff: kopf.Diff) -> bool:
    """Check if a flavor diff contains changes that require recreation.

    Flavors in OpenStack are immutable for core properties. If any of these
    change, we need to delete and recreate the flavor.

    Args:
        diff: Kopf diff object from update handler

    Returns:
        True if the flavor needs to be recreated
    """
    for operation, field, old, new in diff:
        # Check if any immutable field changed
        field_path = ".".join(str(f) for f in field) if isinstance(field, tuple) else str(field)
        for immutable_field in IMMUTABLE_FLAVOR_FIELDS:
            if immutable_field in field_path:
                return True
    return False
