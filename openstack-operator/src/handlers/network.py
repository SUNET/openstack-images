"""Kopf handlers for OpenstackNetwork CRD (provider networks)."""

import logging
import time
from typing import Any

import kopf

from resources.provider_network import (
    delete_provider_network,
    ensure_provider_network,
    get_provider_network_info,
    update_subnet_properties,
)
from state import get_openstack_client, get_registry
from utils import now_iso
from metrics import (
    RECONCILE_TOTAL,
    RECONCILE_DURATION,
    RECONCILE_IN_PROGRESS,
)

logger = logging.getLogger(__name__)


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


@kopf.on.create("sunet.se", "v1alpha1", "openstacknetworks")
def create_network_handler(
    spec: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackNetwork creation."""
    logger.info(f"Creating OpenstackNetwork: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").inc()

    patch.status["phase"] = "Provisioning"
    patch.status["conditions"] = []

    client = get_openstack_client()
    registry = get_registry()

    try:
        network_name = spec["name"]

        _set_patch_condition(patch, "NetworkReady", "False", "Creating", "")

        result = ensure_provider_network(client, spec)

        # Register in ConfigMap with subnet IDs
        subnet_ids = [s["subnetId"] for s in result.get("subnets", [])]
        registry.register(
            "provider_networks",
            network_name,
            result["networkId"],
            cr_name=name,
            extra={"subnets": subnet_ids},
        )

        patch.status["networkId"] = result["networkId"]
        patch.status["subnets"] = result.get("subnets", [])

        _set_patch_condition(patch, "NetworkReady", "True", "Created", "")
        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="create", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackNetwork", operation="create"
        ).observe(duration)
        logger.info(
            f"Successfully created OpenstackNetwork: {name} (id={result['networkId']})"
        )

    except Exception as e:
        logger.error(f"Failed to create OpenstackNetwork {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "NetworkReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="create", status="error"
        ).inc()
        kopf.warn(body, reason="CreateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Creation failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").dec()


@kopf.on.update("sunet.se", "v1alpha1", "openstacknetworks")
def update_network_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    diff: kopf.Diff,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackNetwork updates.

    Provider networks are largely immutable. For significant changes,
    we need to delete and recreate.
    """
    logger.info(f"Updating OpenstackNetwork: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").inc()

    client = get_openstack_client()
    registry = get_registry()
    patch.status["phase"] = "Provisioning"

    # Preserve existing status fields
    for key in ("networkId", "subnets", "conditions"):
        if key in status and key not in patch.status:
            patch.status[key] = status[key]

    try:
        network_name = spec["name"]
        network_id = status.get("networkId")

        if not network_id:
            # No network ID, treat as create
            RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").dec()
            create_network_handler(spec=spec, patch=patch, name=name, body=body)
            return

        # Check what changed - for provider networks, most changes require recreate
        changed_paths = {str(change[1]) for change in diff}
        immutable_network_fields = {
            "providerNetworkType",
            "providerPhysicalNetwork",
            "providerSegmentationId",
            "external",
            "shared",
        }
        # Subnet fields that require recreate (CIDR change = new subnet)
        immutable_subnet_fields = {"cidr"}

        needs_recreate = any(
            field in str(changed_paths) for field in immutable_network_fields
        )

        # Check if subnet changes are only mutable fields
        subnet_changed = "subnets" in str(changed_paths)
        if subnet_changed and not needs_recreate:
            # Only trigger recreate if an existing subnet's immutable field
            # is being changed — not when a new subnet is being added
            for change in diff:
                op = change[0]
                path_str = str(change[1])
                if (
                    op == "change"
                    and "subnets" in path_str
                    and any(f in path_str for f in immutable_subnet_fields)
                ):
                    needs_recreate = True
                    break

        if needs_recreate:
            logger.info(f"Network {name} requires recreate due to property change")
            # Delete old network and subnets
            old_subnets = status.get("subnets", [])
            old_subnet_ids = [s.get("subnetId") for s in old_subnets if s.get("subnetId")]
            delete_provider_network(client, network_id, old_subnet_ids)
            registry.unregister("provider_networks", network_name)

            # Create new one
            result = ensure_provider_network(client, spec)
            subnet_ids = [s["subnetId"] for s in result.get("subnets", [])]
            registry.register(
                "provider_networks",
                network_name,
                result["networkId"],
                cr_name=name,
                extra={"subnets": subnet_ids},
            )

            patch.status["networkId"] = result["networkId"]
            patch.status["subnets"] = result.get("subnets", [])
            _set_patch_condition(patch, "NetworkReady", "True", "Recreated", "")
        elif subnet_changed:
            logger.info(f"Updating subnet properties in-place for {name}")
            subnets_status = update_subnet_properties(
                client, network_id, spec.get("subnets", [])
            )
            patch.status["subnets"] = subnets_status
            _set_patch_condition(patch, "NetworkReady", "True", "Updated", "")
        else:
            _set_patch_condition(patch, "NetworkReady", "True", "Updated", "")

        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="update", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackNetwork", operation="update"
        ).observe(duration)
        logger.info(f"Successfully updated OpenstackNetwork: {name}")

    except Exception as e:
        logger.error(f"Failed to update OpenstackNetwork {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "NetworkReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="update", status="error"
        ).inc()
        kopf.warn(body, reason="UpdateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Update failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").dec()


@kopf.on.delete("sunet.se", "v1alpha1", "openstacknetworks")
def delete_network_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackNetwork deletion."""
    logger.info(f"Deleting OpenstackNetwork: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").inc()

    client = get_openstack_client()
    registry = get_registry()

    network_name = spec["name"]
    network_id = status.get("networkId")

    if not network_id:
        logger.warning(f"No networkId in status for {name}, nothing to delete")
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").dec()
        return

    try:
        subnets = status.get("subnets", [])
        subnet_ids = [s.get("subnetId") for s in subnets if s.get("subnetId")]

        delete_provider_network(client, network_id, subnet_ids)
        registry.unregister("provider_networks", network_name)

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="delete", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackNetwork", operation="delete"
        ).observe(duration)
        logger.info(f"Successfully deleted OpenstackNetwork: {name}")

    except Exception as e:
        logger.error(f"Failed to delete OpenstackNetwork {name}: {e}")
        RECONCILE_TOTAL.labels(
            resource="OpenstackNetwork", operation="delete", status="error"
        ).inc()
        kopf.warn(body, reason="DeleteFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Deletion failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackNetwork").dec()


@kopf.timer("sunet.se", "v1alpha1", "openstacknetworks", interval=300)
def reconcile_network(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    **_: Any,
) -> None:
    """Periodic reconciliation to detect and repair drift."""
    if status.get("phase") != "Ready":
        return

    logger.debug(f"Reconciling OpenstackNetwork: {name}")

    client = get_openstack_client()
    network_name = spec["name"]

    try:
        info = get_provider_network_info(client, network_name)
        if not info:
            logger.warning(f"Network {network_name} not found, triggering recreate")
            patch.status["phase"] = "Pending"
            patch.status["networkId"] = None
            patch.status["subnets"] = []
            return

        if info["network_id"] != status.get("networkId"):
            logger.warning(f"Network ID mismatch for {network_name}")
            patch.status["phase"] = "Pending"
            patch.status["networkId"] = info["network_id"]
            return

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.exception(f"Reconciliation failed for {name}")
