"""Project resource management."""

import logging
from typing import Any

from constants import MANAGED_BY_DESCRIPTION_PREFIX, MANAGED_BY_TAG
from openstack_client import OpenStackClient
from utils import make_group_name

logger = logging.getLogger(__name__)


def ensure_project(
    client: OpenStackClient,
    name: str,
    domain: str,
    description: str = "",
    enabled: bool = True,
) -> tuple[str, str]:
    """Ensure a project and its user group exist.

    Returns:
        Tuple of (project_id, group_id)
    """
    # Check if project exists
    project = client.get_project(name, domain)
    if project:
        logger.info(f"Project {name} already exists with ID {project.id}")
        if project.description != description or project.is_enabled != enabled:
            client.update_project(project.id, description=description, enabled=enabled)
        project_id = project.id
        # Ensure tag exists on existing projects
        client.add_project_tag(project_id, MANAGED_BY_TAG)
    else:
        project = client.create_project(name, domain, description, enabled)
        project_id = project.id
        client.add_project_tag(project_id, MANAGED_BY_TAG)
        logger.info(f"Created project {name} with ID {project_id}")

    # Ensure group exists for project users
    group_name = make_group_name(name)
    group = client.get_group(group_name, domain)
    if group:
        logger.info(f"Group {group_name} already exists with ID {group.id}")
        group_id = group.id
    else:
        # Use description prefix to mark as managed (groups don't support tags)
        group_desc = f"{MANAGED_BY_DESCRIPTION_PREFIX}Users for {name}"
        group = client.create_group(group_name, domain, group_desc)
        group_id = group.id
        logger.info(f"Created group {group_name} with ID {group_id}")

    # Ensure member and reader role assignments
    # reader is an implied role of member, but application credentials
    # require explicit role assignments — implied roles don't count.
    for role_name in ("member", "reader"):
        role = client.get_role(role_name)
        if role:
            client.assign_role_to_group(role.id, group_id, project_id)
        else:
            logger.warning("Role %r not found, skipping role assignment", role_name)

    return project_id, group_id


def delete_project(
    client: OpenStackClient,
    project_id: str,
    group_id: str | None,
    domain: str,
) -> None:
    """Delete a project and its associated group."""
    if group_id:
        try:
            client.delete_group(group_id)
            logger.info(f"Deleted group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to delete group {group_id}: {e}")

    try:
        client.delete_project(project_id)
        logger.info(f"Deleted project {project_id}")
    except Exception as e:
        logger.warning(f"Failed to delete project {project_id}: {e}")


def get_project_info(
    client: OpenStackClient, name: str, domain: str
) -> dict[str, Any] | None:
    """Get project and group information."""
    project = client.get_project(name, domain)
    if not project:
        return None

    group_name = make_group_name(name)
    group = client.get_group(group_name, domain)

    return {
        "project_id": project.id,
        "group_id": group.id if group else None,
    }
