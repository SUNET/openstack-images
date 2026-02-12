"""Kopf handlers for OpenstackFlavor CRD."""

import logging
import time
from typing import Any

import kopf

from resources.flavor import delete_flavor, ensure_flavor, flavor_needs_recreate
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


@kopf.on.create("sunet.se", "v1alpha1", "openstackflavors")
def create_flavor_handler(
    spec: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackFlavor creation."""
    logger.info(f"Creating OpenstackFlavor: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").inc()

    patch.status["phase"] = "Provisioning"
    patch.status["conditions"] = []

    client = get_openstack_client()
    registry = get_registry()

    try:
        flavor_name = spec["name"]

        _set_patch_condition(patch, "FlavorReady", "False", "Creating", "")

        flavor_id = ensure_flavor(client, spec)

        # Register in ConfigMap
        registry.register("flavors", flavor_name, flavor_id, cr_name=name)

        patch.status["flavorId"] = flavor_id
        _set_patch_condition(patch, "FlavorReady", "True", "Created", "")
        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="create", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackFlavor", operation="create"
        ).observe(duration)
        logger.info(f"Successfully created OpenstackFlavor: {name} (id={flavor_id})")

    except Exception as e:
        logger.error(f"Failed to create OpenstackFlavor {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "FlavorReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="create", status="error"
        ).inc()
        kopf.warn(body, reason="CreateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Creation failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").dec()


@kopf.on.update("sunet.se", "v1alpha1", "openstackflavors")
def update_flavor_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    diff: kopf.Diff,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackFlavor updates.

    Note: Flavors are immutable in OpenStack. If core properties change,
    we must delete and recreate the flavor.
    """
    logger.info(f"Updating OpenstackFlavor: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").inc()

    client = get_openstack_client()
    registry = get_registry()
    patch.status["phase"] = "Provisioning"

    # Preserve existing status fields
    for key in ("flavorId", "conditions"):
        if key in status and key not in patch.status:
            patch.status[key] = status[key]

    try:
        flavor_name = spec["name"]
        flavor_id = status.get("flavorId")

        if not flavor_id:
            # No flavor ID, treat as create
            RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").dec()
            create_flavor_handler(spec=spec, patch=patch, name=name)
            return

        # Check if immutable properties changed
        if flavor_needs_recreate(diff):
            logger.info(f"Flavor {name} requires recreate due to immutable property change")
            # Delete old flavor
            delete_flavor(client, flavor_id)
            registry.unregister("flavors", flavor_name)

            # Create new one
            new_flavor_id = ensure_flavor(client, spec)
            registry.register("flavors", flavor_name, new_flavor_id, cr_name=name)

            patch.status["flavorId"] = new_flavor_id
            _set_patch_condition(patch, "FlavorReady", "True", "Recreated", "")
        else:
            # Only extra_specs changed - update those
            extra_specs = spec.get("extraSpecs", {})
            if extra_specs:
                client.set_flavor_extra_specs(flavor_id, extra_specs)
            _set_patch_condition(patch, "FlavorReady", "True", "Updated", "")

        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="update", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackFlavor", operation="update"
        ).observe(duration)
        logger.info(f"Successfully updated OpenstackFlavor: {name}")

    except Exception as e:
        logger.error(f"Failed to update OpenstackFlavor {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "FlavorReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="update", status="error"
        ).inc()
        kopf.warn(body, reason="UpdateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Update failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").dec()


@kopf.on.delete("sunet.se", "v1alpha1", "openstackflavors")
def delete_flavor_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackFlavor deletion."""
    logger.info(f"Deleting OpenstackFlavor: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").inc()

    client = get_openstack_client()
    registry = get_registry()

    flavor_name = spec["name"]
    flavor_id = status.get("flavorId")

    if not flavor_id:
        logger.warning(f"No flavorId in status for {name}, nothing to delete")
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").dec()
        return

    try:
        delete_flavor(client, flavor_id)
        registry.unregister("flavors", flavor_name)

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="delete", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackFlavor", operation="delete"
        ).observe(duration)
        logger.info(f"Successfully deleted OpenstackFlavor: {name}")

    except Exception as e:
        logger.error(f"Failed to delete OpenstackFlavor {name}: {e}")
        RECONCILE_TOTAL.labels(
            resource="OpenstackFlavor", operation="delete", status="error"
        ).inc()
        kopf.warn(body, reason="DeleteFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Deletion failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackFlavor").dec()


@kopf.timer("sunet.se", "v1alpha1", "openstackflavors", interval=300)
def reconcile_flavor(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    **_: Any,
) -> None:
    """Periodic reconciliation to detect and repair drift."""
    if status.get("phase") != "Ready":
        return

    logger.debug(f"Reconciling OpenstackFlavor: {name}")

    client = get_openstack_client()
    flavor_name = spec["name"]

    try:
        flavor = client.get_flavor(flavor_name)
        if not flavor:
            logger.warning(f"Flavor {flavor_name} not found, triggering recreate")
            patch.status["phase"] = "Pending"
            patch.status["flavorId"] = None
            return

        if flavor.id != status.get("flavorId"):
            logger.warning(f"Flavor ID mismatch for {flavor_name}")
            patch.status["phase"] = "Pending"
            patch.status["flavorId"] = flavor.id
            return

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.exception(f"Reconciliation failed for {name}")
