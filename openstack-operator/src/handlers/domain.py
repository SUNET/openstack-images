"""Kopf handlers for OpenstackDomain CRD."""

import logging
import time
from typing import Any

import kopf

from resources.domain import delete_domain, ensure_domain, get_domain_info
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


@kopf.on.create("sunet.se", "v1alpha1", "openstackdomains")
def create_domain_handler(
    spec: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    meta: dict[str, Any],
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackDomain creation."""
    logger.info(f"Creating OpenstackDomain: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").inc()

    patch.status["phase"] = "Provisioning"
    patch.status["conditions"] = []
    patch.status["observedGeneration"] = meta.get("generation", 1)

    client = get_openstack_client()
    registry = get_registry()

    try:
        domain_name = spec.get("name")
        if not domain_name:
            raise kopf.PermanentError("spec.name is required")

        description = spec.get("description", "")
        enabled = spec.get("enabled", True)

        _set_patch_condition(patch, "DomainReady", "False", "Creating", "")

        domain_id = ensure_domain(client, domain_name, description, enabled)

        # Register in ConfigMap
        registry.register("domains", domain_name, domain_id, cr_name=name)

        patch.status["domainId"] = domain_id
        _set_patch_condition(patch, "DomainReady", "True", "Created", "")
        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="create", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackDomain", operation="create"
        ).observe(duration)
        logger.info(f"Successfully created OpenstackDomain: {name} (id={domain_id})")

    except kopf.PermanentError:
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="create", status="permanent_error"
        ).inc()
        raise
    except Exception as e:
        logger.error(f"Failed to create OpenstackDomain {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "DomainReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="create", status="error"
        ).inc()
        kopf.warn(body, reason="CreateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Creation failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").dec()


@kopf.on.update("sunet.se", "v1alpha1", "openstackdomains")
def update_domain_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    meta: dict[str, Any],
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackDomain updates."""
    logger.info(f"Updating OpenstackDomain: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").inc()

    client = get_openstack_client()
    patch.status["phase"] = "Provisioning"
    patch.status["observedGeneration"] = meta.get("generation", 1)

    # Preserve existing status fields
    for key in ("domainId", "conditions"):
        if key in status and key not in patch.status:
            patch.status[key] = status[key]

    try:
        domain_name = spec.get("name")
        if not domain_name:
            raise kopf.PermanentError("spec.name is required")

        description = spec.get("description", "")
        enabled = spec.get("enabled", True)
        domain_id = status.get("domainId")

        if not domain_id:
            # No domain ID, treat as create
            RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").dec()
            create_domain_handler(spec=spec, patch=patch, name=name, meta=meta)
            return

        # Update existing domain
        client.update_domain(domain_id, description=description, enabled=enabled)

        _set_patch_condition(patch, "DomainReady", "True", "Updated", "")
        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="update", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackDomain", operation="update"
        ).observe(duration)
        logger.info(f"Successfully updated OpenstackDomain: {name}")

    except kopf.PermanentError:
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="update", status="permanent_error"
        ).inc()
        raise
    except Exception as e:
        logger.error(f"Failed to update OpenstackDomain {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "DomainReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="update", status="error"
        ).inc()
        kopf.warn(body, reason="UpdateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Update failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").dec()


@kopf.on.delete("sunet.se", "v1alpha1", "openstackdomains")
def delete_domain_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackDomain deletion."""
    logger.info(f"Deleting OpenstackDomain: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").inc()

    client = get_openstack_client()
    registry = get_registry()

    domain_name = spec.get("name", "")
    domain_id = status.get("domainId")

    if not domain_id:
        logger.warning(f"No domainId in status for {name}, nothing to delete")
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").dec()
        return

    try:
        delete_domain(client, domain_id)
        registry.unregister("domains", domain_name)

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="delete", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackDomain", operation="delete"
        ).observe(duration)
        logger.info(f"Successfully deleted OpenstackDomain: {name}")

    except Exception as e:
        logger.error(f"Failed to delete OpenstackDomain {name}: {e}")
        RECONCILE_TOTAL.labels(
            resource="OpenstackDomain", operation="delete", status="error"
        ).inc()
        kopf.warn(body, reason="DeleteFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Deletion failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackDomain").dec()


@kopf.timer("sunet.se", "v1alpha1", "openstackdomains", interval=300)
def reconcile_domain(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    **_: Any,
) -> None:
    """Periodic reconciliation to detect and repair drift."""
    if status.get("phase") != "Ready":
        return

    logger.debug(f"Reconciling OpenstackDomain: {name}")

    client = get_openstack_client()
    domain_name = spec["name"]

    try:
        info = get_domain_info(client, domain_name)
        if not info:
            logger.warning(f"Domain {domain_name} not found, triggering recreate")
            patch.status["phase"] = "Pending"
            patch.status["domainId"] = None
            return

        if info["domain_id"] != status.get("domainId"):
            logger.warning(f"Domain ID mismatch for {domain_name}")
            patch.status["phase"] = "Pending"
            patch.status["domainId"] = info["domain_id"]
            return

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.exception(f"Reconciliation failed for {name}")
