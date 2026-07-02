"""Utility functions for the OpenStack operator."""

import datetime
import re
import uuid
from typing import Any


def is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID.

    Used to detect if a stored group_id is actually a name instead of an ID.
    """
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def sanitize_name(name: str) -> str:
    """Convert a project name to a safe group/resource name.

    Replaces dots and underscores with hyphens, converts to lowercase,
    and removes any characters that aren't alphanumeric or hyphens.

    Example: 'My_Project.Example.COM' -> 'my-project-example-com'
    """
    sanitized = name.replace(".", "-").replace("_", "-").lower()
    sanitized = re.sub(r"[^a-z0-9-]", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)  # collapse multiple hyphens
    return sanitized.strip("-")


def make_group_name(project_name: str) -> str:
    """Generate a group name for a project's users.

    Example: 'my-project.example.com' -> 'my-project-example-com-users'
    """
    return f"{sanitize_name(project_name)}-users"


def now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _cr_identity(cr: dict[str, Any]) -> str:
    """Return 'namespace/name' for a CR dict."""
    meta = cr.get("metadata", {})
    return f"{meta.get('namespace', '')}/{meta.get('name', '')}"


def _cr_precedence_key(cr: dict[str, Any]) -> tuple[str, str, str]:
    """Ordering key for competing CRs: oldest first, then namespace/name."""
    meta = cr.get("metadata", {})
    return (
        meta.get("creationTimestamp") or "",
        meta.get("namespace") or "",
        meta.get("name") or "",
    )


def find_duplicate_project_crs(
    cr_items: list[dict[str, Any]],
    namespace: str,
    name: str,
    project_name: str,
    domain: str,
) -> list[dict[str, Any]]:
    """Find other CRs whose spec targets the same OpenStack project.

    Returns every OpenstackProject CR (except namespace/name itself) with
    the same spec.name and spec.domain — i.e. CRs that would adopt the
    same Keystone project.
    """
    duplicates = []
    for cr in cr_items:
        meta = cr.get("metadata", {})
        if meta.get("namespace") == namespace and meta.get("name") == name:
            continue
        spec = cr.get("spec", {})
        if spec.get("name") == project_name and spec.get("domain") == domain:
            duplicates.append(cr)
    return duplicates


def find_project_owner_cr(
    cr_items: list[dict[str, Any]],
    namespace: str,
    name: str,
    creation_timestamp: str,
    project_name: str,
    domain: str,
) -> str | None:
    """Find another CR with a prior claim on (project_name, domain).

    A competing CR has a prior claim if it already provisioned the project
    (status.projectId set) or is older than this CR. CRs being deleted are
    ignored — their delete handler leaves the shared project alone as long
    as this CR exists.

    Returns the owner's 'namespace/name', or None if this CR may proceed.
    """
    my_key = (creation_timestamp or "", namespace or "", name or "")
    for cr in find_duplicate_project_crs(
        cr_items, namespace, name, project_name, domain
    ):
        if cr.get("metadata", {}).get("deletionTimestamp"):
            continue
        if cr.get("status", {}).get("projectId"):
            return _cr_identity(cr)
        if _cr_precedence_key(cr) < my_key:
            return _cr_identity(cr)
    return None


def set_condition(
    status: dict[str, Any],
    condition_type: str,
    condition_status: str,
    reason: str = "",
    message: str = "",
) -> None:
    """Set or update a condition in the status conditions list."""
    conditions: list[dict[str, str]] = status.setdefault("conditions", [])

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
