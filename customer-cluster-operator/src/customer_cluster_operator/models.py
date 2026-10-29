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
from .inventory_inputs import build_inventory_parameters, validate_profile_revision

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_WORKER_GROUPS = 80


EXTRA_SG_ROLES = ("controller", "worker", "jumphost")
EXTRA_SG_PROTOCOLS = ("tcp", "udp", "icmp", "ipv6-icmp")
EXTRA_SG_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")


def _extra_security_groups(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate spec.network.extraSecurityGroups into a canonical, sorted list."""
    network = spec.get("network")
    if network is None:
        return []
    items = _mapping(network, "spec.network").get("extraSecurityGroups")
    if items in (None, []):
        return []
    if not isinstance(items, list) or len(items) > 10:
        raise ValidationError("spec.network.extraSecurityGroups must be a list of at most 10")
    groups: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(items):
        path = f"spec.network.extraSecurityGroups[{index}]"
        group = _mapping(raw, path)
        name = _required_string(group, "name", path)
        if not EXTRA_SG_NAME.fullmatch(name):
            raise ValidationError(f"{path}.name must be a lowercase DNS-style label (max 32)")
        if name in groups:
            raise ValidationError(f"{path}.name {name!r} is used twice")
        roles = group.get("attachTo")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in EXTRA_SG_ROLES for role in roles)
        ):
            raise ValidationError(f"{path}.attachTo must list roles from {list(EXTRA_SG_ROLES)}")
        rules_in = group.get("rules")
        if not isinstance(rules_in, list) or not rules_in or len(rules_in) > 20:
            raise ValidationError(f"{path}.rules must be a list of 1 to 20 rules")
        rules = []
        for rule_index, raw_rule in enumerate(rules_in):
            rpath = f"{path}.rules[{rule_index}]"
            rule = _mapping(raw_rule, rpath)
            direction, ether = rule.get("direction"), rule.get("etherType")
            if direction not in ("ingress", "egress"):
                raise ValidationError(f"{rpath}.direction must be ingress or egress")
            if ether not in ("IPv4", "IPv6"):
                raise ValidationError(f"{rpath}.etherType must be IPv4 or IPv6")
            protocol = rule.get("protocol")
            if protocol is not None and protocol not in EXTRA_SG_PROTOCOLS:
                raise ValidationError(f"{rpath}.protocol must be one of {list(EXTRA_SG_PROTOCOLS)}")
            # Neutron stores ICMP on IPv6 as ipv6-icmp; keep the declaration in that form so
            # the rule read back equals the declared one.
            if protocol == "icmp" and ether == "IPv6":
                protocol = "ipv6-icmp"
            if protocol == "ipv6-icmp" and ether != "IPv6":
                raise ValidationError(f"{rpath}.protocol ipv6-icmp needs etherType IPv6")
            low, high = rule.get("portRangeMin"), rule.get("portRangeMax")
            if low is not None or high is not None:
                if protocol not in ("tcp", "udp"):
                    raise ValidationError(f"{rpath} ports need protocol tcp or udp")
                if low is None:
                    low = high
                if high is None:
                    high = low
                if (
                    any(type(port) is not int or not 1 <= port <= 65535 for port in (low, high))
                    or low > high
                ):
                    raise ValidationError(f"{rpath} has an invalid port range")
            prefix = rule.get("remoteIpPrefix")
            if prefix is not None:
                try:
                    network_prefix = ipaddress.ip_network(prefix, strict=False)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{rpath}.remoteIpPrefix is not a valid CIDR") from exc
                if network_prefix.version != (4 if ether == "IPv4" else 6):
                    raise ValidationError(f"{rpath}.remoteIpPrefix does not match etherType")
                prefix = str(network_prefix)
            rules.append(
                {
                    "direction": direction,
                    "etherType": ether,
                    "protocol": protocol,
                    "portRangeMin": low,
                    "portRangeMax": high,
                    "remoteIpPrefix": prefix,
                    "description": str(rule.get("description") or "").strip()[:255],
                }
            )
        groups[name] = {
            "name": name,
            "description": str(group.get("description") or "").strip()[:255],
            "attachTo": sorted(set(roles)),
            "rules": sorted(
                {json.dumps(rule, sort_keys=True): rule for rule in rules}.values(),
                key=lambda rule: json.dumps(rule, sort_keys=True),
            ),
        }
    return [groups[name] for name in sorted(groups)]


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
        """Preserve the exact v1 hash used to protect already-provisioned resources."""
        infrastructure = dict(self.data)
        if infrastructure.get("schemaVersion") == 2:
            infrastructure.pop("inventory")
            infrastructure.pop("profileRevision")
            infrastructure.pop("extraSecurityGroups", None)
            infrastructure.pop("flavorOverrides", None)
            infrastructure["schemaVersion"] = 1
        encoded = json.dumps(infrastructure, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @property
    def inventory_hash(self) -> str:
        encoded = json.dumps(self.data["inventory"], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @property
    def publication_hash(self) -> str:
        """Complete immutable Job input identity, including publication-only changes."""
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()

    @property
    def inventory_path(self) -> str:
        return f"clusters/{self.data['cluster']['slug']}/generated/ansible/hosts.yml"

    @property
    def policy_inventory_path(self) -> str:
        return f"inventory/clusters/{self.data['cluster']['slug']}.yml"


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
    profile_revision: dict[str, Any],
    uid: str,
    slug: str,
    namespace: str,
    project_id: str,
    operator_namespace: str,
) -> ProvisioningInput:
    """Build the worker envelope, separating policy from the v1 infrastructure hash."""
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

    vip_values: dict[str, str] = {}
    for key in ("apiVipAddress", "ingressVipAddress"):
        value = _required_string(network, key, "profile.spec.network")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValidationError(f"profile.spec.network.{key} is invalid") from exc
        if address.version != 4 or address not in cidr:
            raise ValidationError(f"profile.spec.network.{key} is outside the CIDR")
        if address in {cidr.network_address, cidr.broadcast_address}:
            raise ValidationError(f"profile.spec.network.{key} is the network or broadcast address")
        vip_values[key] = str(address)
    if vip_values["apiVipAddress"] == vip_values["ingressVipAddress"]:
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
        "schemaVersion": 2,
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
        "inventory": build_inventory_parameters(spec, profile, profile_name(spec)),
        "profileRevision": validate_profile_revision(profile_revision),
    }
    overrides = {}
    for role, key in (
        ("controller", "controllerFlavor"),
        ("worker", "workerFlavor"),
        ("jumphost", "jumphostFlavor"),
    ):
        if openstack_spec.get(key) is not None:
            overrides[role] = _required_string(openstack_spec, key, "spec.openstack")
    if overrides:
        # Declared per cluster, not part of the immutable infrastructure hash (see input_hash);
        # data["openstack"][role]["flavor"] keeps the ClusterProfile value for that hash.
        data["flavorOverrides"] = overrides
    extra_groups = _extra_security_groups(spec)
    if extra_groups:
        # Not part of the immutable infrastructure hash (see input_hash); present only when
        # declared, so existing clusters keep their exact publication hash.
        data["extraSecurityGroups"] = extra_groups
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
