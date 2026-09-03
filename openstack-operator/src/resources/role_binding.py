"""Role binding management for OpenStack projects."""

import logging
import os
from typing import Any

from openstack_client import OpenStackClient

logger = logging.getLogger(__name__)

# Operator-side allowlist of OpenStack roles that an OpenstackProject CR may
# request. Defence-in-depth: even if the CRD schema is loosened or a CR
# bypasses validation, the operator refuses to bind anything outside this
# set. `admin` is intentionally excluded; cluster-admin granting must happen
# out-of-band by SUNET ops, not via tenant-mutable CRs.
_DEFAULT_ALLOWED_ROLES = (
    "reader",
    "member",
    "load-balancer_member",
    "heat_stack_user",
    "heat_stack_owner",
)


def _allowed_roles() -> frozenset[str]:
    override = os.environ.get("ALLOWED_OPENSTACK_ROLES", "").strip()
    if override:
        return frozenset(r.strip() for r in override.split(",") if r.strip())
    return frozenset(_DEFAULT_ALLOWED_ROLES)


def apply_role_bindings(
    client: OpenStackClient,
    project_id: str,
    group_id: str,
    role_bindings: list[dict[str, Any]],
    project_domain: str,
    *,
    managed: bool = False,
) -> None:
    """Apply role bindings to a project.

    **Default (managed=False)** behavior — for self-service customer projects:
    binding.users are added to the project's federation group, the binding's
    role is assigned to that group. Federation mapping then admits the user
    on first SSO login.

    **managed=True** behavior — for SUNET-managed projects (e.g. the OpenStack
    project that hosts a tenant K8s cluster's VMs/storage): roles are assigned
    *directly* to each user in binding.users via Keystone, with no project-
    group indirection. This is right for the read-only customer-admin access
    pattern: customer admins should see the project in Horizon but not be in
    the project's working group. Users not yet in Keystone (federated users
    who have never logged in) are skipped and retried on the next reconcile.

    Args:
        client: OpenStack client
        project_id: Project ID
        group_id: Project's user group ID (only used when managed=False)
        role_bindings: List of role binding specifications
        project_domain: Domain of the project
        managed: Whether this is a SUNET-managed project (changes assignment
            mode to direct user-role rather than via the project group)
    """
    if not role_bindings:
        logger.debug(f"No role bindings specified for project {project_id}")
        return

    allowed = _allowed_roles()
    for binding in role_bindings:
        role_name = binding["role"]
        if role_name not in allowed:
            logger.error(
                "Refusing to bind disallowed role %r on project %s "
                "(allowed: %s)",
                role_name, project_id, sorted(allowed),
            )
            continue
        role = client.get_role(role_name)
        if not role:
            logger.warning(f"Role {role_name} not found, skipping")
            continue

        users = binding.get("users", [])
        user_domain = binding.get("userDomain", project_domain)
        groups = binding.get("groups", [])
        group_domain = binding.get("groupDomain", project_domain)

        # Group bindings are applied identically in both modes.
        for group_name in groups:
            group = client.get_group(group_name, group_domain)
            if group:
                client.assign_role_to_group(role.id, group.id, project_id)
                logger.info(
                    f"Assigned role {role_name} to group {group_name} "
                    f"on project {project_id}"
                )
            else:
                logger.warning(
                    f"Group {group_name} not found in domain {group_domain}"
                )

        if managed:
            # Direct user-role assignment, no project-group involvement.
            _sync_user_role_assignments(
                client, role.id, users, user_domain, project_id
            )
        else:
            # Project-group-based: assign role to project group + sync users in.
            if group_id:
                client.assign_role_to_group(role.id, group_id, project_id)
                logger.info(
                    f"Assigned role {role_name} to project group {group_id} "
                    f"on project {project_id}"
                )
                _sync_users_to_group(client, users, user_domain, group_id)


def _sync_user_role_assignments(
    client: OpenStackClient,
    role_id: str,
    desired_users: list[str],
    user_domain: str,
    project_id: str,
) -> None:
    """Idempotently ensure direct user-role assignments on a project.

    Direct Keystone assignments have no ownership metadata, so removing an
    assignment absent from one binding could revoke another binding or a
    manual assignment. Managed-project reconciliation is therefore
    deliberately add-only. Users that don't yet exist in Keystone (e.g.
    federated users who haven't logged in) are retried on the next reconcile.
    """
    for username in desired_users:
        user = client.get_user(username, user_domain)
        if user:
            client.assign_role_to_user(role_id, user.id, project_id)
        else:
            logger.debug(
                f"User {username} not found in domain {user_domain}, "
                "will be assigned after first SSO login"
            )


def _sync_users_to_group(
    client: OpenStackClient,
    desired_users: list[str],
    user_domain: str,
    group_id: str,
) -> None:
    """Sync group membership to match desired users.

    Adds users that should be in the group and removes users that shouldn't.
    Users are identified by their OIDC sub claim (used as username).
    Users that don't exist yet are skipped - they'll be added on next
    reconciliation after their first SSO login.
    """
    # Get current group members
    current_members = client.list_group_users(group_id)
    current_usernames = {user.name for user in current_members}

    # Add users that should be in the group
    for username in desired_users:
        if username not in current_usernames:
            user = client.get_user(username, user_domain)
            if user:
                client.add_user_to_group(user.id, group_id)
                logger.info(f"Added user {username} to group {group_id}")
            else:
                logger.debug(
                    f"User {username} not found in domain {user_domain}, "
                    "will be added after first SSO login"
                )

    # Remove users that shouldn't be in the group
    desired_set = set(desired_users)
    for user in current_members:
        if user.name not in desired_set:
            client.remove_user_from_group(user.id, group_id)
            logger.info(f"Removed user {user.name} from group {group_id}")


def get_users_from_role_bindings(
    role_bindings: list[dict[str, Any]],
) -> list[str]:
    """Extract all users from role bindings.

    These users will be added to the federation mapping.
    """
    users: list[str] = []
    for binding in role_bindings:
        binding_users = binding.get("users", [])
        for user in binding_users:
            if user not in users:
                users.append(user)
    return users
