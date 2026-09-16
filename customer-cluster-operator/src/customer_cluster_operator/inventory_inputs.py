"""Validated, non-secret policy inputs, distinct from immutable infrastructure."""

import ipaddress
import re
from pathlib import PurePosixPath
from typing import Any, TypedDict

from .errors import ValidationError

INVENTORY_KEYS = frozenset({
    "profileName", "apiHostname", "argocdHostname", "nodeInterface", "pythonInterpreter",
})
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class ProfileRevision(TypedDict):
    uid: str
    generation: int


def validate_profile_revision(value: Any) -> ProfileRevision:
    """Keep only server-supplied profile identity and spec revision in the envelope."""
    if not isinstance(value, dict):
        raise ValidationError("profileRevision must contain a profile UID and generation")
    uid = _text(value.get("uid"), "profileRevision.uid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", uid):
        raise ValidationError("profileRevision.uid is invalid")
    generation = value.get("generation")
    if type(generation) is not int or generation < 1:
        raise ValidationError("profileRevision.generation must be a positive integer")
    return {"uid": uid, "generation": generation}


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{path} must be a non-empty string without surrounding whitespace")
    return value


def _hostname(value: object, path: str) -> str:
    hostname = _text(value, path)
    if (
        len(hostname) > 253 or len(hostname.split(".")) < 2
        or any(not DNS_LABEL.fullmatch(label) for label in hostname.split("."))
    ):
        raise ValidationError(f"{path} must be a lowercase ASCII DNS hostname")
    if re.fullmatch(r"[0-9]+(?:[.][0-9]+){3}", hostname):
        raise ValidationError(f"{path} must be a DNS hostname, not an IP address")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    raise ValidationError(f"{path} must be a DNS hostname, not an IP address")


def validate_inventory_parameters(value: Any) -> dict[str, str]:
    """Return exactly the allowlisted publication inputs, never arbitrary spec fields."""
    if not isinstance(value, dict) or set(value) != INVENTORY_KEYS:
        raise ValidationError("inventory must contain the five required publication policy fields")
    result = {key: _text(value[key], f"inventory.{key}") for key in INVENTORY_KEYS}
    if len(result["profileName"]) > 63 or not DNS_LABEL.fullmatch(result["profileName"]):
        raise ValidationError("inventory.profileName must be a DNS label")
    for key in ("apiHostname", "argocdHostname"):
        result[key] = _hostname(result[key], f"inventory.{key}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", result["nodeInterface"]):
        raise ValidationError("inventory.nodeInterface must be a literal Linux interface name")
    interpreter = result["pythonInterpreter"]
    path = PurePosixPath(interpreter)
    if (
        len(interpreter) > 512 or not path.is_absolute() or interpreter.startswith("//")
        or str(path) != interpreter or ".." in path.parts
        or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", interpreter)
        or not re.fullmatch(r"python3(?:[.][0-9]+)?", path.name)
    ):
        raise ValidationError("inventory.pythonInterpreter must be an absolute Python 3 path")
    return result


def build_inventory_parameters(
    spec: dict[str, Any], profile: dict[str, Any], profile_name: str,
) -> dict[str, str]:
    dns = spec.get("dns")
    ansible = profile.get("ansible")
    if not isinstance(dns, dict):
        raise ValidationError("spec.dns canonical API and Argo CD hostnames are required")
    if not isinstance(ansible, dict):
        raise ValidationError(
            "profile.spec.ansible reviewed interface and interpreter are required"
        )
    return validate_inventory_parameters({
        "profileName": profile_name,
        "apiHostname": dns.get("apiHostname"),
        "argocdHostname": dns.get("argocdHostname"),
        "nodeInterface": ansible.get("nodeInterface"),
        "pythonInterpreter": ansible.get("pythonInterpreter"),
    })
