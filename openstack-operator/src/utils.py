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
