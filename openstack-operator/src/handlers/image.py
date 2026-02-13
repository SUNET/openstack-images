"""Kopf handlers for OpenstackImage CRD."""

import datetime
import logging
import time
from typing import Any

import kopf

from resources.image import delete_image, ensure_image, ensure_image_settings, get_image_status
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


@kopf.on.create("sunet.se", "v1alpha1", "openstackimages")
def create_image_handler(
    spec: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackImage creation."""
    logger.info(f"Creating OpenstackImage: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").inc()

    patch.status["phase"] = "Provisioning"
    patch.status["conditions"] = []

    client = get_openstack_client()
    registry = get_registry()
    is_external = spec.get("external", False)

    try:
        image_name = spec["name"]

        if is_external:
            # External image: only manage settings on existing images
            _set_patch_condition(patch, "ImageReady", "False", "Configuring", "")

            result = ensure_image_settings(client, spec)

            if result is None:
                # Image doesn't exist
                _set_patch_condition(
                    patch, "ImageReady", "False", "NotFound",
                    f"External image '{image_name}' not found in OpenStack"
                )
                patch.status["phase"] = "Pending"
                patch.status["lastSyncTime"] = now_iso()
                logger.warning(f"External image {name} not found, will retry")
                raise kopf.TemporaryError(f"External image not found: {image_name}", delay=60)

            image_id, upload_status = result
            # Don't register external images for garbage collection
            patch.status["imageId"] = image_id
            patch.status["uploadStatus"] = upload_status
            _set_patch_condition(patch, "ImageReady", "True", "Configured", "")
            patch.status["phase"] = "Ready"
            patch.status["lastSyncTime"] = now_iso()
            logger.info(f"Configured external OpenstackImage: {name} (id={image_id})")

        else:
            # Managed image: create if needed
            _set_patch_condition(patch, "ImageReady", "False", "Creating", "")

            # Create image and start import (async operation)
            image_id, upload_status = ensure_image(client, spec)

            # Register in ConfigMap for garbage collection
            registry.register("images", image_name, image_id, cr_name=name)

            patch.status["imageId"] = image_id
            patch.status["uploadStatus"] = upload_status

            if upload_status == "active":
                _set_patch_condition(patch, "ImageReady", "True", "Active", "")
                patch.status["phase"] = "Ready"
            else:
                _set_patch_condition(
                    patch, "ImageReady", "False", "Importing",
                    f"Image import in progress (status: {upload_status})"
                )
                patch.status["importStartedAt"] = now_iso()
                patch.status["importRetries"] = 0
                # Keep phase as Provisioning until import completes

            patch.status["lastSyncTime"] = now_iso()

            duration = time.monotonic() - start_time
            RECONCILE_TOTAL.labels(
                resource="OpenstackImage", operation="create", status="success"
            ).inc()
            RECONCILE_DURATION.labels(
                resource="OpenstackImage", operation="create"
            ).observe(duration)
            logger.info(f"Created OpenstackImage: {name} (id={image_id}, status={upload_status})")

    except kopf.TemporaryError:
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="create", status="error"
        ).inc()
        raise
    except Exception as e:
        logger.error(f"Failed to create OpenstackImage {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "ImageReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="create", status="error"
        ).inc()
        kopf.warn(body, reason="CreateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Creation failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()


@kopf.on.update("sunet.se", "v1alpha1", "openstackimages")
def update_image_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackImage updates.

    Note: Only metadata (visibility, protected, tags, properties) can be updated.
    Changing the content (URL) requires delete and recreate.
    """
    logger.info(f"Updating OpenstackImage: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").inc()

    client = get_openstack_client()
    patch.status["phase"] = "Provisioning"

    # Preserve existing status fields
    for key in ("imageId", "uploadStatus", "checksum", "sizeBytes", "conditions"):
        if key in status and key not in patch.status:
            patch.status[key] = status[key]

    try:
        image_id = status.get("imageId")

        if not image_id:
            # No image ID, treat as create
            RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()
            create_image_handler(spec=spec, patch=patch, name=name)
            return

        # Update mutable properties
        visibility = spec.get("visibility", "private")
        protected = spec.get("protected", False)
        tags = spec.get("tags", [])
        properties = spec.get("properties", {})

        client.update_image(
            image_id,
            visibility=visibility,
            protected=protected,
            tags=tags,
            properties=properties,
        )

        _set_patch_condition(patch, "ImageReady", "True", "Updated", "")
        patch.status["phase"] = "Ready"
        patch.status["lastSyncTime"] = now_iso()

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="update", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackImage", operation="update"
        ).observe(duration)
        logger.info(f"Successfully updated OpenstackImage: {name}")

    except Exception as e:
        logger.error(f"Failed to update OpenstackImage {name}: {e}")
        patch.status["phase"] = "Error"
        _set_patch_condition(patch, "ImageReady", "False", "Error", str(e)[:200])
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="update", status="error"
        ).inc()
        kopf.warn(body, reason="UpdateFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Update failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()


@kopf.on.delete("sunet.se", "v1alpha1", "openstackimages")
def delete_image_handler(
    spec: dict[str, Any],
    status: dict[str, Any],
    name: str,
    body: kopf.Body,
    **_: Any,
) -> None:
    """Handle OpenstackImage deletion."""
    logger.info(f"Deleting OpenstackImage: {name}")
    start_time = time.monotonic()
    RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").inc()

    is_external = spec.get("external", False)
    image_name = spec["name"]

    if is_external:
        # External images are not deleted - we don't own them
        logger.info(f"Skipping deletion of external image {name} (not owned by operator)")
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()
        return

    client = get_openstack_client()
    registry = get_registry()

    image_id = status.get("imageId")

    if not image_id:
        logger.warning(f"No imageId in status for {name}, nothing to delete")
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()
        return

    try:
        delete_image(client, image_id)
        registry.unregister("images", image_name)

        duration = time.monotonic() - start_time
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="delete", status="success"
        ).inc()
        RECONCILE_DURATION.labels(
            resource="OpenstackImage", operation="delete"
        ).observe(duration)
        logger.info(f"Successfully deleted OpenstackImage: {name}")

    except Exception as e:
        logger.error(f"Failed to delete OpenstackImage {name}: {e}")
        RECONCILE_TOTAL.labels(
            resource="OpenstackImage", operation="delete", status="error"
        ).inc()
        kopf.warn(body, reason="DeleteFailed", message=str(e)[:200])
        raise kopf.TemporaryError(f"Deletion failed: {e}", delay=60)
    finally:
        RECONCILE_IN_PROGRESS.labels(resource="OpenstackImage").dec()


@kopf.timer("sunet.se", "v1alpha1", "openstackimages", interval=30)
def poll_image_status(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    **_: Any,
) -> None:
    """Poll image upload status until import completes.

    This timer runs every 30 seconds to check the import status.
    Once the image reaches 'active' status, the phase changes to Ready.
    """
    # Only poll if we're still provisioning
    current_phase = status.get("phase")
    if current_phase not in ("Provisioning", "Pending"):
        return

    image_id = status.get("imageId")
    if not image_id:
        return

    logger.debug(f"Polling image status for {name}")

    client = get_openstack_client()

    try:
        image_status = get_image_status(client, image_id)

        if image_status is None:
            logger.warning(f"Image {name} not found, triggering recreate")
            patch.status["phase"] = "Pending"
            patch.status["imageId"] = None
            patch.status["uploadStatus"] = None
            return

        patch.status["uploadStatus"] = image_status["status"]
        if image_status.get("checksum"):
            patch.status["checksum"] = image_status["checksum"]
        if image_status.get("size"):
            patch.status["sizeBytes"] = image_status["size"]

        if image_status["status"] == "active":
            logger.info(f"Image {name} import completed successfully")
            _set_patch_condition(patch, "ImageReady", "True", "Active", "")
            patch.status["phase"] = "Ready"
        elif image_status["status"] in ("killed", "deleted"):
            logger.error(f"Image {name} import failed with status: {image_status['status']}")
            _set_patch_condition(
                patch, "ImageReady", "False", "ImportFailed",
                f"Image status: {image_status['status']}"
            )
            patch.status["phase"] = "Error"
        else:
            # Still importing (queued, saving, importing)
            _set_patch_condition(
                patch, "ImageReady", "False", "Importing",
                f"Image status: {image_status['status']}"
            )

            # Detect stuck imports: only timeout on "queued" status
            # (importing/saving indicate active progress)
            if image_status["status"] == "queued":
                import_started = status.get("importStartedAt")
                if not import_started:
                    # Pre-existing CR from before this fix; give it a fresh window
                    patch.status["importStartedAt"] = now_iso()
                else:
                    started_dt = datetime.datetime.fromisoformat(import_started)
                    elapsed = (datetime.datetime.now(datetime.UTC) - started_dt).total_seconds()
                    if elapsed > 600:
                        retries = status.get("importRetries", 0)
                        logger.warning(
                            f"Image {name} stuck in queued for {elapsed:.0f}s "
                            f"(retry {retries + 1}/3), deleting"
                        )
                        try:
                            client.delete_image(image_id)
                        except Exception:
                            logger.exception(f"Failed to delete stuck image {image_id}")
                        retries += 1
                        if retries < 3:
                            patch.status["phase"] = "Pending"
                            patch.status["imageId"] = None
                            patch.status["uploadStatus"] = None
                            patch.status["importStartedAt"] = None
                            patch.status["importRetries"] = retries
                        else:
                            patch.status["phase"] = "Error"
                            _set_patch_condition(
                                patch, "ImageReady", "False", "ImportFailed",
                                f"Image stuck in queued after {retries} retries"
                            )

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.error(f"Failed to poll image status for {name}: {e}")


@kopf.timer("sunet.se", "v1alpha1", "openstackimages", interval=300)
def reconcile_image(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    name: str,
    **_: Any,
) -> None:
    """Periodic reconciliation to detect and repair drift."""
    current_phase = status.get("phase")
    is_external = spec.get("external", False)

    # For images in Pending state, retry finding or recreating them
    if current_phase == "Pending":
        client = get_openstack_client()
        if is_external:
            logger.debug(f"Retrying external image lookup for {name}")
            result = ensure_image_settings(client, spec)
            if result:
                image_id, upload_status = result
                patch.status["imageId"] = image_id
                patch.status["uploadStatus"] = upload_status
                patch.status["phase"] = "Ready"
                _set_patch_condition(patch, "ImageReady", "True", "Configured", "")
                patch.status["lastSyncTime"] = now_iso()
                logger.info(f"External image {name} found and configured")
        else:
            # Managed image disappeared — recreate it
            logger.info(f"Recreating managed image {name}")
            registry = get_registry()
            try:
                image_id, upload_status = ensure_image(client, spec)
                registry.register("images", spec["name"], image_id, cr_name=name)
                patch.status["imageId"] = image_id
                patch.status["uploadStatus"] = upload_status
                if upload_status == "active":
                    _set_patch_condition(patch, "ImageReady", "True", "Active", "")
                    patch.status["phase"] = "Ready"
                else:
                    _set_patch_condition(
                        patch, "ImageReady", "False", "Importing",
                        f"Image import in progress (status: {upload_status})"
                    )
                    patch.status["importStartedAt"] = now_iso()
                    patch.status["phase"] = "Provisioning"
                patch.status["lastSyncTime"] = now_iso()
                logger.info(f"Recreated image {name} (id={image_id}, status={upload_status})")
            except Exception as e:
                logger.error(f"Failed to recreate image {name}: {e}")
                patch.status["phase"] = "Error"
                _set_patch_condition(patch, "ImageReady", "False", "Error", str(e)[:200])
        return

    if current_phase != "Ready":
        return

    logger.debug(f"Reconciling OpenstackImage: {name}")

    client = get_openstack_client()
    image_name = spec["name"]

    try:
        image = client.get_image(image_name)
        if not image:
            if is_external:
                # External image disappeared - go back to Pending
                logger.warning(f"External image {image_name} not found")
                patch.status["phase"] = "Pending"
                patch.status["imageId"] = None
                _set_patch_condition(
                    patch, "ImageReady", "False", "NotFound",
                    f"External image '{image_name}' not found in OpenStack"
                )
            else:
                # Managed image - trigger recreate
                logger.warning(f"Image {image_name} not found, triggering recreate")
                patch.status["phase"] = "Pending"
                patch.status["imageId"] = None
            return

        if image.id != status.get("imageId"):
            logger.warning(f"Image ID mismatch for {image_name}")
            patch.status["phase"] = "Pending"
            patch.status["imageId"] = image.id
            return

        # Ensure settings are still correct
        visibility = spec.get("visibility", "private")
        protected = spec.get("protected", False)

        if image.visibility != visibility or image.is_protected != protected:
            logger.info(f"Drift detected for {image_name}, updating settings")
            client.update_image(
                image.id,
                visibility=visibility,
                protected=protected,
                tags=spec.get("tags", []),
                properties=spec.get("properties", {}),
            )

        patch.status["lastSyncTime"] = now_iso()

    except Exception as e:
        logger.exception(f"Reconciliation failed for {name}")
