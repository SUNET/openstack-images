"""Kopf handlers for OpenstackProject CRD."""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add src directory to path for imports when run as script by Kopf
_src_dir = Path(__file__).parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

import kopf
from kubernetes import client as k8s_client
from prometheus_client import start_http_server

from resources.federation import FederationManager
from resources.network import delete_networks, ensure_networks
from resources.project import delete_project, ensure_project, get_project_info
from resources.quota import apply_quotas
from resources.garbage_collection import (
    collect_garbage,
    get_expected_projects_from_crs,
    get_federation_config_from_crs,
)
from notification_listener import start_notification_listener
from resources.role_binding import apply_role_bindings, get_users_from_role_bindings
from resources.security_group import delete_security_groups, ensure_security_groups
from state import state, get_openstack_client, get_registry
from utils import is_valid_uuid, make_group_name, now_iso
from metrics import (
    RECONCILE_TOTAL,
    RECONCILE_DURATION,
    RECONCILE_IN_PROGRESS,
    PROJECT_GC_RUNS,
    PROJECT_GC_DELETED_RESOURCES,
    PROJECT_GC_DURATION,
    set_operator_info,
    init_metrics,
)

# Import cluster-scoped resource handlers (registers with Kopf)
import handlers  # noqa: F401

logger = logging.getLogger(__name__)

# Operator version
OPERATOR_VERSION = "0.1.0"


def _set_patch_condition(
    patch: kopf.Patch,
    condition_type: str,
    condition_status: str,
    reason: str = "",
    message: str = "",
) -> None:
    """Set or update a condition in patch.status.conditions."""
    if "conditions" not in patch.status:
        patch.status["conditions"] = []

    conditions: list[dict[str, str]] = patch.status["conditions"]

    for condition in conditions:
        if condition["type"] == condition_type:
            if condition["status"] != condition_status:
                condition["status"] = condition_status
                condition["lastTransitionTime"] = now_iso()
            condition["reason"] = reason
            condition["message"] = message
            return

    conditions.append(
        {
            "type": condition_type,
            "status": condition_status,
            "reason": reason,
            "message": message,
            "lastTransitionTime": now_iso(),
        }
    )


def _resolve_group_id(
    client: Any,
    group_id: str | None,
    project_name: str,
    domain: str,
    patch: kopf.Patch,
) -> str | None:
    """Resolve a group identifier to an actual group ID.

    Handles the case where status.groupId contains a group name instead of
    a UUID (legacy data or manual edits). If the stored value is not a valid
    UUID, it will look up the group by deriving the name from the project name.

    If the group ID is corrected, also updates patch.status["groupId"] so the
    fix is persisted.

    Args:
        client: OpenStack client
        group_id: The stored group_id (may be a name or UUID)
        project_name: Project name (used to derive expected group name)
        domain: Domain for group lookup
        patch: Kopf patch object to update status if group_id is corrected

    Returns:
        The resolved group ID (UUID), or None if group not found
    """
    if not group_id:
        return None

    # If it's a valid UUID, verify the group exists and return it
    if is_valid_uuid(group_id):
        group = client.get_group_by_id(group_id)
        if group:
            return group_id
        # Group ID is a UUID but group doesn't exist - try to find by name
        logger.warning(
            "Group with ID %s not found, attempting to find by name", group_id
        )

    # The stored group_id is not a UUID or group not found by ID
    # Try to find the group by deriving the name from project name
    expected_group_name = make_group_name(project_name)
    logger.info(
        "Resolving group by name: %s (stored value was: %s)",
        expected_group_name,
        group_id,
    )

    group = client.get_group(expected_group_name, domain)
    if group:
        logger.info(
            "Resolved group %s to ID %s (correcting stored value)",
            expected_group_name,
            group.id,
        )
        # Update the patch so the correct ID is persisted
        patch.status["groupId"] = group.id
        return group.id

    logger.warning(
        "Could not resolve group for project %s (expected name: %s)",
        project_name,
        expected_group_name,
    )
    return None


def get_federation_config(
    namespace: str, config_ref: dict[str, Any] | None
) -> dict[str, str] | None:
    """Load federation config from ConfigMap.

    Args:
        namespace: Namespace of the OpenstackProject CR
        config_ref: federationRef from the CR spec

    Returns:
        Dict with idp_name, idp_remote_id, sso_domain, or None
    """
    if not config_ref:
        return None

    cm_name = config_ref.get("configMapName")
    cm_namespace = config_ref.get("configMapNamespace", namespace)

    if not cm_name:
        return None

    v1 = state.get_k8s_core_api()
    try:
        cm = v1.read_namespaced_config_map(cm_name, cm_namespace)
        return {
            "idp_name": cm.data.get("idp-name", ""),
            "idp_remote_id": cm.data.get("idp-remote-id", ""),
            "sso_domain": cm.data.get("sso-domain", ""),
        }
    except k8s_client.ApiException as e:
        logger.error(f"Failed to read ConfigMap {cm_namespace}/{cm_name}: {e}")
        return None


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Configure operator settings on startup."""
    # Reduce logging noise
    settings.posting.level = logging.WARNING
    # Configure persistence
    settings.persistence.finalizer = "sunet.se/openstack-operator"
    # Set watching namespace - explicit cluster-wide or specific namespace
    # Can be overridden by WATCH_NAMESPACE env var
    watch_namespace = os.environ.get("WATCH_NAMESPACE", "")
    if watch_namespace:
        settings.watching.namespaces = [watch_namespace]
    else:
        settings.watching.clusterwide = True

    # Start Prometheus metrics server
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))
    try:
        start_http_server(metrics_port)
        logger.info("Prometheus metrics server started on port %d", metrics_port)
    except OSError as e:
        logger.warning("Failed to start metrics server on port %d: %s", metrics_port, e)

    # Initialize metrics and set operator info
    cloud_name = os.environ.get("OS_CLOUD", "openstack")
    init_metrics()
    set_operator_info(OPERATOR_VERSION, cloud_name)

    logger.info("OpenStack operator started (version %s)", OPERATOR_VERSION)

    # Start Keystone notification listener if configured
    transport_url = os.environ.get("NOTIFICATION_TRANSPORT_URL")
    if transport_url:
        loop = asyncio.get_event_loop()
        loop.create_task(_start_notification_listener(transport_url))
        logger.info("Keystone notification listener started")
    else:
        logger.info("NOTIFICATION_TRANSPORT_URL not set, notification listener disabled")


async def _start_notification_listener(transport_url: str) -> None:
    """Launch the notification listener with a user-sync callback."""

    async def on_user_created(user_id: str) -> None:
        """Triggered when Keystone creates a new user (e.g. federated shadow user)."""
        client = get_openstack_client()

        # Look up the user by ID to get username and domain
        try:
            user = client.conn.identity.get_user(user_id)
        except Exception:
            logger.exception("Failed to look up user %s", user_id)
            return
        if not user:
            return

        username = user.name
        user_domain = client.conn.identity.get_domain(user.domain_id)
        domain_name = user_domain.name if user_domain else "unknown"

        logger.info("New user %s in domain %s, syncing groups", username, domain_name)

        # Find all OpenstackProject CRs that reference this user
        api = state.get_k8s_custom_api()
        try:
            crs = api.list_cluster_custom_object(
                group="sunet.se", version="v1alpha1", plural="openstackprojects"
            )
        except Exception:
            logger.exception("Failed to list OpenstackProject CRs")
            return

        for cr in crs.get("items", []):
            spec = cr.get("spec", {})
            cr_status = cr.get("status", {})
            group_id = cr_status.get("groupId")
            if not group_id or cr_status.get("phase") != "Ready":
                continue

            project_id = cr_status.get("projectId")
            role_bindings = spec.get("roleBindings", [])
            domain = spec.get("domain", "sso-users")

            for binding in role_bindings:
                if username in binding.get("users", []):
                    try:
                        apply_role_bindings(
                            client, project_id, group_id, role_bindings, domain
                        )
                        logger.info(
                            "Synced user %s to project %s",
                            username,
                            spec.get("name"),
                        )
                    except Exception:
                        logger.exception(
                            "Failed to sync user %s to project %s",
                            username,
                            spec.get("name"),
                        )
                    break

    await start_notification_listener(transport_url, on_user_created)


@kopf.on.cleanup()
def cleanup(**_: Any) -> None:
    """Clean up resources on operator shutdown."""
    logger.info("OpenStack operator shutting down")
    state.close()


@kopf.on.create("sunet.se", "v1alpha1", "openstackprojects")
def create_project(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    meta: dict[str, Any],
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackProject creation."""
    logger.info(f"Creating OpenstackProject: {namespace}/{name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").inc()

    # Update status to Provisioning - use patch.status directly
    patch.status["phase"] = "Provisioning"
    patch.status["conditions"] = []
    patch.status["observedGeneration"] = meta.get("generation", 1)

    client = get_openstack_client()

    try:
        # Validate required fields
        project_name = spec.get("name")
        domain = spec.get("domain")
        if not project_name or not domain:
            raise kopf.PermanentError("spec.name and spec.domain are required")

        description = spec.get("description", "")
        enabled = spec.get("enabled", True)

        # 1. Create project and group
        _set_patch_condition(patch, "ProjectReady", "False", "Creating", "")
        project_id, group_id = ensure_project(
            client, project_name, domain, description, enabled
        )
        patch.status["projectId"] = project_id
        patch.status["groupId"] = group_id
        _set_patch_condition(patch, "ProjectReady", "True", "Created", "")

        # 2. Apply quotas
        quotas = spec.get("quotas", {})
        if quotas:
            _set_patch_condition(patch, "QuotasReady", "False", "Applying", "")
            apply_quotas(client, project_id, quotas)
            _set_patch_condition(patch, "QuotasReady", "True", "Applied", "")

        # 3. Create networks
        networks = spec.get("networks", [])
        if networks:
            _set_patch_condition(patch, "NetworksReady", "False", "Creating", "")
            network_statuses = ensure_networks(client, project_id, networks)
            patch.status["networks"] = network_statuses
            _set_patch_condition(patch, "NetworksReady", "True", "Created", "")

        # 4. Create security groups
        security_groups = spec.get("securityGroups", [])
        if security_groups:
            _set_patch_condition(patch, "SecurityGroupsReady", "False", "Creating", "")
            sg_statuses = ensure_security_groups(client, project_id, security_groups)
            patch.status["securityGroups"] = sg_statuses
            _set_patch_condition(patch, "SecurityGroupsReady", "True", "Created", "")

        # 5. Apply role bindings
        role_bindings = spec.get("roleBindings", [])
        if role_bindings:
            apply_role_bindings(client, project_id, group_id, role_bindings, domain)

        # 6. Update federation mapping
        federation_ref = spec.get("federationRef")
        if federation_ref and role_bindings:
            fed_config = get_federation_config(namespace, federation_ref)
            if fed_config and fed_config["idp_name"]:
                _set_patch_condition(patch, "FederationReady", "False", "Configuring", "")
                users = get_users_from_role_bindings(role_bindings)
                if users:
                    manager = FederationManager(
                        client,
                        fed_config["idp_name"],
                        fed_config["idp_remote_id"],
                        fed_config["sso_domain"],
                    )
                    manager.add_project_mapping(project_name, users)
                    # Register federation mapping for GC tracking
                    registry = get_registry()
                    registry.register(
                        "federation_mappings",
                        project_name,
                        manager.mapping_name,
                        name,  # CR name
                        {"idp_name": fed_config["idp_name"]},
                    )
                _set_patch_condition(patch, "FederationReady", "True", "Configured", "")

        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        # Record success metrics
        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="create", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackProject", operation="create"
        ).observe(duration)
        logger.info(f"Successfully created OpenstackProject: {namespace}/{name}")

    except kopf.PermanentError:
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="create", status="permanent_error"
        ).inc()
        raise
    except Exception as e:
        logger.error(f"Failed to create OpenstackProject {namespace}/{name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "Ready", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="create", status="error"
        ).inc()
        kopf.warn(body, reason="CreateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Creation failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").dec()


@kopf.on.update("sunet.se", "v1alpha1", "openstackprojects")
def update_project(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    meta: dict[str, Any],
    diff: kopf.Diff,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackProject updates."""
    logger.info(f"Updating OpenstackProject: {namespace}/{name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").inc()

    client = get_openstack_client()
    patch.status["phase"] = "Provisioning"
    patch.status["observedGeneration"] = meta.get("generation", 1)

    # Preserve existing status fields
    for key in ("projectId", "groupId", "networks", "securityGroups", "conditions"):
        if key in status and key not in patch.status:
            patch.status[key] = status[key]

    try:
        # Validate required fields
        project_name = spec.get("name")
        domain = spec.get("domain")
        if not project_name or not domain:
            raise kopf.PermanentError("spec.name and spec.domain are required")

        project_id = status.get("projectId")
        group_id = status.get("groupId")

        # Resolve group_id if it's not a valid UUID (legacy data fix)
        group_id = _resolve_group_id(client, group_id, project_name, domain, patch)

        # If we don't have project_id, treat as create
        if not project_id:
            RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").dec()
            create_project(
                spec=spec,
                status=status,
                patch=patch,
                namespace=namespace,
                name=name,
                meta=meta,
            )
            return

        # Check what changed and update accordingly
        changed_paths = {change[1] for change in diff}

        # Update project description/enabled if changed
        if any(p[0] in ("description", "enabled") for p in changed_paths):
            description = spec.get("description", "")
            enabled = spec.get("enabled", True)
            client.update_project(project_id, description=description, enabled=enabled)

        # Update quotas if changed
        if any("quotas" in str(p) for p in changed_paths):
            quotas = spec.get("quotas", {})
            apply_quotas(client, project_id, quotas)
            _set_patch_condition(patch, "QuotasReady", "True", "Updated", "")

        # Update networks if changed
        if any("networks" in str(p) for p in changed_paths):
            networks = spec.get("networks", [])
            # Delete old networks and create new ones
            old_networks = status.get("networks", [])
            delete_networks(client, old_networks)
            if networks:
                network_statuses = ensure_networks(client, project_id, networks)
                patch.status["networks"] = network_statuses
            else:
                patch.status["networks"] = []
            _set_patch_condition(patch, "NetworksReady", "True", "Updated", "")

        # Update security groups if changed
        if any("securityGroups" in str(p) for p in changed_paths):
            security_groups = spec.get("securityGroups", [])
            # Delete old security groups and create new ones
            old_sgs = status.get("securityGroups", [])
            delete_security_groups(client, old_sgs)
            if security_groups:
                sg_statuses = ensure_security_groups(
                    client, project_id, security_groups
                )
                patch.status["securityGroups"] = sg_statuses
            else:
                patch.status["securityGroups"] = []
            _set_patch_condition(patch, "SecurityGroupsReady", "True", "Updated", "")

        # Always apply role bindings and federation to ensure consistency
        # This handles cases where the spec hasn't changed but state needs repair
        role_bindings = spec.get("roleBindings", [])
        if role_bindings:
            apply_role_bindings(
                client, project_id, group_id, role_bindings, domain
            )

        # Always update federation mapping
        federation_ref = spec.get("federationRef")
        if federation_ref:
            fed_config = get_federation_config(namespace, federation_ref)
            if fed_config and fed_config["idp_name"]:
                users = get_users_from_role_bindings(role_bindings)
                manager = FederationManager(
                    client,
                    fed_config["idp_name"],
                    fed_config["idp_remote_id"],
                    fed_config["sso_domain"],
                )
                registry = get_registry()
                if users:
                    manager.add_project_mapping(project_name, users)
                    # Register federation mapping for GC tracking
                    registry.register(
                        "federation_mappings",
                        project_name,
                        manager.mapping_name,
                        name,  # CR name
                        {"idp_name": fed_config["idp_name"]},
                    )
                else:
                    # Remove mapping when no users remain
                    manager.remove_project_mapping(project_name)
                    registry.unregister("federation_mappings", project_name)
                _set_patch_condition(patch, "FederationReady", "True", "Updated", "")

        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        # Record success metrics
        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="update", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackProject", operation="update"
        ).observe(duration)
        logger.info(f"Successfully updated OpenstackProject: {namespace}/{name}")

    except kopf.PermanentError:
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="update", status="permanent_error"
        ).inc()
        raise
    except Exception as e:
        logger.error(f"Failed to update OpenstackProject {namespace}/{name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "Ready", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="update", status="error"
        ).inc()
        kopf.warn(body, reason="UpdateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Update failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").dec()


@kopf.on.delete("sunet.se", "v1alpha1", "openstackprojects")
def delete_project_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackProject deletion."""
    logger.info(f"Deleting OpenstackProject: {namespace}/{name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").inc()

    client = get_openstack_client()

    project_name = spec.get("name", "")
    domain = spec.get("domain", "")
    project_id = status.get("projectId")
    group_id = status.get("groupId")

    if not project_id:
        logger.warning(
            f"No project_id in status for {namespace}/{name}, nothing to delete"
        )
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").dec()
        return

    try:
        # 1. Remove federation mapping
        federation_ref = spec.get("federationRef")
        if federation_ref:
            fed_config = get_federation_config(namespace, federation_ref)
            if fed_config and fed_config["idp_name"]:
                manager = FederationManager(
                    client,
                    fed_config["idp_name"],
                    fed_config["idp_remote_id"],
                    fed_config["sso_domain"],
                )
                manager.remove_project_mapping(project_name)
                # Unregister from GC tracking
                registry = get_registry()
                registry.unregister("federation_mappings", project_name)

        # 2. Delete security groups
        sg_statuses = status.get("securityGroups", [])
        if sg_statuses:
            delete_security_groups(client, sg_statuses)

        # 3. Delete networks (routers, subnets, networks)
        network_statuses = status.get("networks", [])
        if network_statuses:
            delete_networks(client, network_statuses)

        # 4. Delete project and group
        delete_project(client, project_id, group_id, domain)

        # Record success metrics
        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="delete", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackProject", operation="delete"
        ).observe(duration)
        logger.info(f"Successfully deleted OpenstackProject: {namespace}/{name}")

    except Exception as e:
        logger.error(f"Failed to delete OpenstackProject {namespace}/{name}: {e}")
        RECONCILE_TOTAL.labels(
            resource="OpenstackProject", operation="delete", status="error"
        ).inc()
        kopf.warn(body, reason="DeleteFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Deletion failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackProject").dec()


@kopf.timer("sunet.se", "v1alpha1", "openstackprojects", interval=300)
def reconcile_project(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    **_: Any,
) -> None:
    """Periodic reconciliation to detect and repair drift."""
    if status.get("phase") != "Ready":
        logger.debug(
            f"Skipping reconciliation for {namespace}/{name}: phase is "
            f"{status.get('phase')}"
        )
        return

    logger.debug(f"Reconciling OpenstackProject: {namespace}/{name}")

    client = get_openstack_client()
    project_name = spec["name"]
    domain = spec["domain"]

    try:
        # Verify project exists
        info = get_project_info(client, project_name, domain)
        if not info:
            logger.warning(
                f"Project {project_name} not found in OpenStack, triggering recreate"
            )
            # Clear status to trigger recreation
            patch.status["phase"] = "Pending"
            patch.status["projectId"] = None
            patch.status["groupId"] = None
            return

        # Verify project ID matches
        if info["project_id"] != status.get("projectId"):
            logger.warning(
                f"Project ID mismatch for {project_name}: "
                f"expected {status.get('projectId')}, got {info['project_id']}"
            )
            patch.status["phase"] = "Pending"
            patch.status["projectId"] = info["project_id"]
            patch.status["groupId"] = info["group_id"]
            return

        # Re-apply role bindings to sync group membership
        # Users may not have existed at CR creation time (federated users
        # are only created on first SSO login), so we retry here.
        role_bindings = spec.get("roleBindings", [])
        if role_bindings:
            project_id = status.get("projectId")
            group_id = _resolve_group_id(
                client, status.get("groupId"), project_name, domain, patch
            )
            if project_id and group_id:
                apply_role_bindings(
                    client, project_id, group_id, role_bindings, domain
                )

        # Verify and repair federation resources if configured
        federation_ref = spec.get("federationRef")
        if federation_ref and role_bindings:
            fed_config = get_federation_config(namespace, federation_ref)
            if fed_config and fed_config["idp_name"]:
                users = get_users_from_role_bindings(role_bindings)
                if users:
                    manager = FederationManager(
                        client,
                        fed_config["idp_name"],
                        fed_config["idp_remote_id"],
                        fed_config["sso_domain"],
                    )
                    # This will create IdP/mapping/protocol if missing
                    manager.add_project_mapping(project_name, users)
                    # Ensure federation mapping is registered for GC tracking
                    registry = get_registry()
                    registry.register(
                        "federation_mappings",
                        project_name,
                        manager.mapping_name,
                        name,  # CR name
                        {"idp_name": fed_config["idp_name"]},
                    )

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.exception(f"Reconciliation failed for {namespace}/{name}")


@kopf.daemon("sunet.se", "v1alpha1", "openstackprojects", cancellation_timeout=10)
async def garbage_collector(
    name: str,
    namespace: str,
    stopped: kopf.DaemonStopped,
    **_: Any,
) -> None:
    """Periodic garbage collection daemon.

    Runs every 10 minutes to clean up orphaned resources in OpenStack
    that don't have corresponding OpenstackProject CRs.

    This daemon attaches to each CR but only the "leader" (first CR
    alphabetically) actually runs GC to avoid duplicate work.
    """
    gc_interval = int(os.environ.get("GC_INTERVAL_SECONDS", "600"))
    managed_domain = os.environ.get("MANAGED_DOMAIN", "sso-users")
    my_identity = f"{namespace}/{name}"

    # Ensure k8s config is loaded once
    state.get_k8s_core_api()

    while not stopped:
        try:
            # List all OpenstackProject CRs
            api = k8s_client.CustomObjectsApi()
            crs = api.list_cluster_custom_object(
                group="sunet.se",
                version="v1alpha1",
                plural="openstackprojects",
            )

            cr_items = crs.get("items", [])
            if not cr_items:
                logger.debug("No OpenstackProject CRs found, skipping GC")
                await stopped.wait(gc_interval)
                continue

            # Simple leader election: only the first CR (alphabetically) runs GC
            sorted_crs = sorted(
                cr_items,
                key=lambda x: (
                    x.get("metadata", {}).get("namespace", ""),
                    x.get("metadata", {}).get("name", ""),
                ),
            )
            first_cr = sorted_crs[0]
            leader_identity = (
                f"{first_cr.get('metadata', {}).get('namespace', '')}/"
                f"{first_cr.get('metadata', {}).get('name', '')}"
            )

            # Only run GC if we're the leader
            if my_identity != leader_identity:
                logger.debug(
                    f"GC skipped on {my_identity}, leader is {leader_identity}"
                )
                await stopped.wait(gc_interval)
                continue

            logger.info(f"Running garbage collection for domain {managed_domain}")
            gc_start_time = time.monotonic()

            # Get expected projects from all CRs
            expected_projects = get_expected_projects_from_crs(cr_items)
            logger.debug(f"Expected projects: {expected_projects}")

            # Get federation config for cleaning up orphaned mappings
            core_api = state.get_k8s_core_api()
            federation_config = get_federation_config_from_crs(cr_items, core_api)

            # Run GC
            client = get_openstack_client()
            result = collect_garbage(
                client, managed_domain, expected_projects, federation_config
            )

            gc_duration = time.monotonic() - gc_start_time
            PROJECT_GC_DURATION.observe(gc_duration)

            deleted_projects = len(result.get("deleted_projects", []))
            deleted_groups = len(result.get("deleted_groups", []))
            deleted_mappings = len(result.get("deleted_mappings", []))

            if deleted_projects or deleted_groups or deleted_mappings:
                PROJECT_GC_DELETED_RESOURCES.labels(resource_type="project").inc(deleted_projects)
                PROJECT_GC_DELETED_RESOURCES.labels(resource_type="group").inc(deleted_groups)
                PROJECT_GC_DELETED_RESOURCES.labels(resource_type="mapping").inc(deleted_mappings)
                logger.info(
                    f"GC completed: deleted {deleted_projects} projects, "
                    f"{deleted_groups} groups, {deleted_mappings} mappings"
                )
            else:
                logger.debug("GC completed: no orphaned resources found")

            PROJECT_GC_RUNS.labels(status="success").inc()

        except Exception as e:
            logger.error(f"Garbage collection failed: {e}")
            PROJECT_GC_RUNS.labels(status="error").inc()

        await stopped.wait(gc_interval)


def main() -> None:
    """Entry point for running the operator."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Kopf will be run via the CLI, but this allows direct invocation for testing
    logger.info("Starting OpenStack operator...")
    logger.info("Use 'kopf run src/handlers.py' to run the operator")
    sys.exit(0)


if __name__ == "__main__":
    main()
