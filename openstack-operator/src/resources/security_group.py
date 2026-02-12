"""Security group management."""

import logging
from typing import Any

from constants import MANAGED_BY_TAG
from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)

# Tags to apply to all created resources
_RESOURCE_TAGS = [MANAGED_BY_TAG]


def ensure_security_group(
    client: OpenStackClient,
    project_id: str,
    sg_spec: dict[str, Any],
    all_security_groups: dict[str, str] | None = None,
) -> dict[str, str]:
    """Ensure a security group exists with the specified rules.

    Args:
        client: OpenStack client
        project_id: Project ID to create resources in
        sg_spec: Security group specification from CR
        all_security_groups: Dict mapping sg names to IDs for remote group refs

    Returns:
        Dict with name and id
    """
    name = sg_spec["name"]
    description = sg_spec.get("description", "")
    rules = sg_spec.get("rules", [])

    result: dict[str, str] = {"name": name}

    # Create or get security group
    sg = client.get_security_group(name, project_id)
    if sg:
        logger.info(f"Security group {name} already exists with ID {sg.id}")
        result["id"] = sg.id
    else:
        sg = client.create_security_group(name, project_id, description, tags=_RESOURCE_TAGS)
        result["id"] = sg.id
        logger.info(f"Created security group {name} with ID {sg.id}")

    # Create rules
    for rule_spec in rules:
        remote_group_id = None
        remote_group_name = rule_spec.get("remoteGroupName")
        if remote_group_name and all_security_groups:
            remote_group_id = all_security_groups.get(remote_group_name)
            if not remote_group_id:
                logger.warning(
                    f"Remote security group {remote_group_name} not found, "
                    "skipping rule"
                )
                continue

        client.create_security_group_rule(
            security_group_id=sg.id,
            direction=rule_spec["direction"],
            protocol=rule_spec.get("protocol"),
            port_range_min=rule_spec.get("portRangeMin"),
            port_range_max=rule_spec.get("portRangeMax"),
            remote_ip_prefix=rule_spec.get("remoteIpPrefix"),
            remote_group_id=remote_group_id,
            ethertype=rule_spec.get("ethertype", "IPv4"),
        )

    return result


def delete_security_group(
    client: OpenStackClient,
    sg_status: dict[str, str],
) -> None:
    """Delete a security group."""
    sg_id = sg_status.get("id")
    if sg_id:
        try:
            client.delete_security_group(sg_id)
        except Exception as e:
            logger.warning(f"Failed to delete security group {sg_id}: {e}")


def ensure_security_groups(
    client: OpenStackClient,
    project_id: str,
    sg_specs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Ensure all specified security groups exist.

    Creates groups in two passes:
    1. First pass: create all groups without rules
    2. Second pass: create rules (allows cross-group references)

    Returns:
        List of security group status dicts
    """
    results: list[dict[str, str]] = []
    sg_name_to_id: dict[str, str] = {}

    # First pass: create security groups without rules
    for spec in sg_specs:
        name = spec["name"]
        description = spec.get("description", "")

        sg = client.get_security_group(name, project_id)
        if sg:
            logger.info(f"Security group {name} already exists with ID {sg.id}")
            sg_id = sg.id
        else:
            sg = client.create_security_group(name, project_id, description, tags=_RESOURCE_TAGS)
            sg_id = sg.id
            logger.info(f"Created security group {name} with ID {sg.id}")

        results.append({"name": name, "id": sg_id})
        sg_name_to_id[name] = sg_id

    # Second pass: create rules
    for spec in sg_specs:
        name = spec["name"]
        sg_id = sg_name_to_id[name]
        rules = spec.get("rules", [])

        for rule_spec in rules:
            remote_group_id = None
            remote_group_name = rule_spec.get("remoteGroupName")
            if remote_group_name:
                remote_group_id = sg_name_to_id.get(remote_group_name)
                if not remote_group_id:
                    logger.warning(
                        f"Remote security group {remote_group_name} not found"
                    )
                    continue

            client.create_security_group_rule(
                security_group_id=sg_id,
                direction=rule_spec["direction"],
                protocol=rule_spec.get("protocol"),
                port_range_min=rule_spec.get("portRangeMin"),
                port_range_max=rule_spec.get("portRangeMax"),
                remote_ip_prefix=rule_spec.get("remoteIpPrefix"),
                remote_group_id=remote_group_id,
                ethertype=rule_spec.get("ethertype", "IPv4"),
            )

    return results


def delete_security_groups(
    client: OpenStackClient,
    sg_statuses: list[dict[str, str]],
) -> None:
    """Delete all security groups from status."""
    for status in sg_statuses:
        delete_security_group(client, status)
