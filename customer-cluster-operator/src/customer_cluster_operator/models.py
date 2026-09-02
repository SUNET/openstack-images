"""Validation and immutable provisioning input construction."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .constants import DEFAULT_PROFILE
from .errors import ValidationError

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_WORKER_GROUPS = 80


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    return value


def _required_string(value: dict[str, Any], key: str, path: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValidationError(f"{path}.{key} must be a non-empty string")
    return item.strip()


def _positive_int(value: dict[str, Any], key: str, path: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
        raise ValidationError(f"{path}.{key} must be a positive integer")
    return item


def _namespaced_ref(
    value: Any, path: str, operator_namespace: str, *, require_key: bool = True
) -> dict[str, str]:
    ref = _mapping(value, path)
    namespace = ref.get("namespace", operator_namespace)
    if namespace != operator_namespace:
        raise ValidationError(f"{path}.namespace must be {operator_namespace}")
    result = {
        "name": _required_string(ref, "name", path),
        "namespace": operator_namespace,
    }
    if require_key:
        result["key"] = _required_string(ref, "key", path)
    return result


@dataclass(frozen=True)
class ProvisioningInput:
    data: dict[str, Any]

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"))

    @property
    def input_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()

    @property
    def inventory_path(self) -> str:
        return f"clusters/{self.data['cluster']['slug']}/generated/ansible/hosts.yml"


def profile_name(spec: dict[str, Any]) -> str:
    ref = spec.get("profileRef") or {}
    if not isinstance(ref, dict):
        raise ValidationError("spec.profileRef must be an object")
    name = ref.get("name", DEFAULT_PROFILE)
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("spec.profileRef.name must be a non-empty string")
    return name.strip()


def is_suspended(spec: dict[str, Any]) -> bool:
    if spec.get("deletionPolicy", "Retain") != "Retain":
        raise ValidationError("only deletionPolicy Retain is supported")
    value = spec.get("suspend", False)
    if not isinstance(value, bool):
        raise ValidationError("spec.suspend must be a boolean")
    return value


def build_input(
    *,
    spec: dict[str, Any],
    profile: dict[str, Any],
    uid: str,
    slug: str,
    namespace: str,
    project_id: str,
    operator_namespace: str,
) -> ProvisioningInput:
    """Validate API data and return only infrastructure-affecting fields."""
    if namespace != operator_namespace:
        raise ValidationError(f"ManagedCluster namespace must be {operator_namespace}")
    if not uid:
        raise ValidationError("ManagedCluster metadata.uid is required")
    if not DNS_LABEL.fullmatch(slug) or len(slug) > 63:
        raise ValidationError("ManagedCluster name must be a valid DNS label")
    if is_suspended(spec):
        raise ValidationError("a suspended ManagedCluster cannot be provisioned")

    for key in ("displayName", "contractNumber", "customerDomain"):
        _required_string(spec, key, "spec")
    worker_groups = _positive_int(spec, "workerGroups", "spec")
    max_worker_groups = _positive_int(profile, "maxWorkerGroups", "profile.spec")
    if max_worker_groups > MAX_WORKER_GROUPS:
        raise ValidationError(f"profile.spec.maxWorkerGroups must not exceed {MAX_WORKER_GROUPS}")
    if worker_groups > max_worker_groups:
        raise ValidationError("spec.workerGroups must not exceed profile.spec.maxWorkerGroups")

    openstack_spec = _mapping(spec.get("openstack"), "spec.openstack")
    project_name = _required_string(openstack_spec, "projectName", "spec.openstack")
    _required_string(openstack_spec, "projectResourceName", "spec.openstack")

    _required_string(profile, "projectNamespace", "profile.spec")

    os_profile = _mapping(profile.get("openstack"), "profile.spec.openstack")
    controller = _mapping(os_profile.get("controller"), "profile.spec.openstack.controller")
    worker = _mapping(os_profile.get("worker"), "profile.spec.openstack.worker")
    jumphost = _mapping(os_profile.get("jumphost"), "profile.spec.openstack.jumphost")
    network = _mapping(profile.get("network"), "profile.spec.network")
    ssh = _mapping(profile.get("ssh"), "profile.spec.ssh")
    git = _mapping(profile.get("git"), "profile.spec.git")

    cidr_text = _required_string(network, "cidr", "profile.spec.network")
    try:
        cidr = ipaddress.ip_network(cidr_text, strict=True)
    except ValueError as exc:
        raise ValidationError("profile.spec.network.cidr must be a valid network") from exc
    if cidr.version != 4:
        raise ValidationError("only IPv4 cluster networks are supported")
    required_addresses = 3 * max_worker_groups + 11
    usable_addresses = max(cidr.num_addresses - 2, 0)
    if required_addresses > usable_addresses:
        raise ValidationError(
            "profile.spec.maxWorkerGroups is unsafe for profile.spec.network.cidr capacity"
        )

    nameservers = network.get("dnsNameservers")
    if not isinstance(nameservers, list) or not nameservers:
        raise ValidationError("profile.spec.network.dnsNameservers must be a non-empty list")
    for value in nameservers:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValidationError(f"invalid DNS nameserver {value!r}") from exc

    vip_values: dict[str, str | None] = {}
    for key in ("apiVipAddress", "ingressVipAddress"):
        value = network.get(key)
        if value is not None:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValidationError(f"profile.spec.network.{key} is invalid") from exc
            if address not in cidr:
                raise ValidationError(f"profile.spec.network.{key} is outside the CIDR")
            value = str(address)
        vip_values[key] = value
    if vip_values["apiVipAddress"] == vip_values["ingressVipAddress"]:
        if vip_values["apiVipAddress"] is not None:
            raise ValidationError("API and ingress VIP addresses must differ")

    allowed = network.get("sshAllowedCIDRs")
    if not isinstance(allowed, list) or not allowed:
        raise ValidationError("profile.spec.network.sshAllowedCIDRs must be non-empty")
    try:
        allowed_networks = [ipaddress.ip_network(item, strict=False) for item in allowed]
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "profile.spec.network.sshAllowedCIDRs contains an invalid CIDR"
        ) from exc
    if any(item.version != 4 for item in allowed_networks):
        raise ValidationError("profile.spec.network.sshAllowedCIDRs must contain only IPv4 CIDRs")
    allowed = [str(item) for item in allowed_networks]

    repo_url = _required_string(git, "repoUrl", "profile.spec.git")
    parsed = urlsplit(repo_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("profile.spec.git.repoUrl must be HTTPS without embedded credentials")

    def machine(source: dict[str, Any], path: str) -> dict[str, Any]:
        return {
            "flavor": _required_string(source, "flavor", path),
            "rootVolumeGB": _positive_int(source, "rootVolumeGB", path),
        }

    data = {
        "schemaVersion": 1,
        "cluster": {"uid": uid, "slug": slug},
        "project": {"id": project_id, "name": project_name},
        "openstack": {
            "cloud": _required_string(os_profile, "cloud", "profile.spec.openstack"),
            "image": _required_string(os_profile, "image", "profile.spec.openstack"),
            "externalNetwork": _required_string(
                os_profile, "externalNetwork", "profile.spec.openstack"
            ),
            "credentialsSecret": _namespaced_ref(
                os_profile.get("credentialsSecret"),
                "profile.spec.openstack.credentialsSecret",
                operator_namespace,
            ),
            "controller": machine(controller, "profile.spec.openstack.controller"),
            "worker": machine(worker, "profile.spec.openstack.worker"),
            "jumphost": machine(jumphost, "profile.spec.openstack.jumphost"),
        },
        "network": {
            "cidr": str(cidr),
            "dnsNameservers": nameservers,
            "apiVipAddress": vip_values["apiVipAddress"],
            "ingressVipAddress": vip_values["ingressVipAddress"],
            "sshAllowedCIDRs": allowed,
        },
        "ssh": {
            "authorizedKeysConfigMap": _namespaced_ref(
                ssh.get("authorizedKeysConfigMap"),
                "profile.spec.ssh.authorizedKeysConfigMap",
                operator_namespace,
            )
        },
        "git": {
            "repoUrl": repo_url,
            "branch": _required_string(git, "branch", "profile.spec.git"),
            "username": _required_string(git, "username", "profile.spec.git"),
            "tokenSecret": _namespaced_ref(
                git.get("tokenSecret"),
                "profile.spec.git.tokenSecret",
                operator_namespace,
            ),
        },
        "nodes": {"controllers": 3, "workers": 3 * worker_groups},
    }
    return ProvisioningInput(deepcopy(data))


def job_name(
    slug: str,
    uid: str,
    input_hash: str,
    generation: int = 1,
    verification_bucket: int = 0,
) -> str:
    suffix = f"-{uid.replace('-', '')[:8]}-{input_hash[:10]}-g{generation}-v{verification_bucket}"
    prefix = slug[: 57 - len("mc--provision") - len(suffix)].rstrip("-")
    return f"mc-{prefix}-provision{suffix}"
