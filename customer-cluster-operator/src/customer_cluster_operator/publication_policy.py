"""Publication input validation, YAML ownership, and source freshness checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from .constants import API_GROUP, API_VERSION, DEFAULT_PROFILE, MANAGED_BY
from .errors import InventoryConflict, ValidationError
from .inventory_inputs import DNS_LABEL, validate_inventory_parameters

HOSTS_MARKER = (
    "# GENERATED FILE: customer-cluster-operator. Manual edits will be overwritten."
)
POLICY_MARKER = "# GENERATED FILE: customer-cluster-operator cluster policy."
_OWNERSHIP_COMMENT = re.compile(
    # Bare keys also identify truncated ownership headers.
    r"^\s*(?:GENERATED\s+FILE|owner|formatVersion|ManagedClusterUID)\s*(?::|$)",
    re.IGNORECASE,
)


class _UniqueLoader(yaml.SafeLoader):
    """Safe YAML, including legacy SafeDumper aliases, without ambiguous keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("duplicate YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_document(content: bytes) -> dict[str, Any]:
    """Reject unsafe/ambiguous YAML without exposing file contents in errors."""
    if len(content) > 2_000_000:
        raise InventoryConflict("Publication YAML exceeds the supported size")
    try:
        result = yaml.load(content.decode("utf-8"), Loader=_UniqueLoader)  # noqa: S506
        if not isinstance(result, dict):
            raise ValueError("expected a mapping")
        return result
    except (UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise InventoryConflict("Publication YAML is invalid or ambiguous") from None


def _markers(content: bytes) -> list[str]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError:
        raise InventoryConflict("Publication YAML is not UTF-8") from None
    return [
        line for line in lines
        if "#" in line and _OWNERSHIP_COMMENT.search(line.split("#", 1)[1])
    ]


@dataclass(frozen=True)
class PolicyInputs:
    slug: str
    uid: str
    parameters: dict[str, str]

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> PolicyInputs:
        if not isinstance(data, dict) or not isinstance(data.get("cluster"), dict):
            raise ValidationError("Publication cluster identity is required")
        slug, uid = data["cluster"].get("slug"), data["cluster"].get("uid")
        if not isinstance(slug, str) or len(slug) > 63 or not DNS_LABEL.fullmatch(slug):
            raise ValidationError("Publication cluster name must be a DNS label")
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", uid):
            raise ValidationError("Publication ManagedCluster UID is invalid")
        return cls(slug, uid, validate_inventory_parameters(data.get("inventory")))

    @property
    def header(self) -> str:
        return (
            f"---\n{POLICY_MARKER}\n"
            f"# owner: {MANAGED_BY}\n"
            "# formatVersion: 1\n"
            f"# ManagedClusterUID: {self.uid}\n"
        )

    @property
    def document(self) -> dict[str, Any]:
        return {"all": {"vars": {
            "customer_cluster_name": self.slug,
            "customer_cluster_profile": self.parameters["profileName"],
            "customer_cluster_node_interface": self.parameters["nodeInterface"],
            "customer_cluster_api_hostname": self.parameters["apiHostname"],
            "customer_cluster_argocd_hostname": self.parameters["argocdHostname"],
            "ansible_python_interpreter": self.parameters["pythonInterpreter"],
        }}}

    def render(self) -> str:
        return self.header + yaml.safe_dump(self.document, sort_keys=False)


def render_cluster_policy(provisioning_data: dict[str, Any]) -> str:
    """Render only the six reviewed policy variables and explicit ownership."""
    return PolicyInputs.from_data(provisioning_data).render()


def validate_hosts(content: bytes) -> None:
    """Hosts retain the original operator marker, including pre-schema2 files."""
    if (
        _markers(content) != [HOSTS_MARKER]
        or not content.startswith(f"---\n{HOSTS_MARKER}\n".encode())
    ):
        raise InventoryConflict("Hosts inventory is not unambiguously operator-owned")
    document = load_document(content)
    if set(document) != {"all"} or not isinstance(document["all"], dict):
        raise InventoryConflict("Operator hosts inventory has an invalid structure")


def policy_content(existing: bytes | None, expected: PolicyInputs) -> bytes:
    """Preserve matching manual policy verbatim; never implicitly adopt it."""
    generated = expected.render().encode()
    if existing is None:
        return generated
    document = load_document(existing)
    markers = _markers(existing)
    if markers:
        if (
            not existing.startswith(expected.header.encode())
            or markers != expected.header.splitlines()[1:]
        ):
            raise InventoryConflict("Cluster policy ownership, UID, or format is ambiguous")
        return generated
    if document != expected.document:
        raise InventoryConflict("Manual cluster policy differs from the expected policy")
    return existing


def validate_source(content: bytes | None, data: dict[str, Any], policy: PolicyInputs) -> None:
    """Check the current Git declaration against the immutable worker envelope."""
    project, nodes = data.get("project"), data.get("nodes")
    if not isinstance(project, dict) or not isinstance(nodes, dict):
        raise ValidationError("Publication project and node inputs are required")
    project_name, workers = project.get("name"), nodes.get("workers")
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValidationError("Publication project name is required")
    if type(workers) is not int or workers <= 0 or workers % 3:
        raise ValidationError("Publication workers must be a positive multiple of three")
    openstack = data.get("openstack")
    credentials_secret = openstack.get("credentialsSecret") if isinstance(openstack, dict) else None
    expected_namespace = (
        credentials_secret.get("namespace") if isinstance(credentials_secret, dict) else None
    )
    if (
        not isinstance(expected_namespace, str) or len(expected_namespace) > 63
        or not DNS_LABEL.fullmatch(expected_namespace)
    ):
        raise ValidationError("Publication expected namespace must be a DNS label")
    if content is None:
        raise InventoryConflict("ManagedCluster declaration is missing at checkout HEAD")
    declaration = load_document(content)
    metadata, spec = declaration.get("metadata"), declaration.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise InventoryConflict("ManagedCluster declaration is invalid at checkout HEAD")
    dns, project_spec = spec.get("dns"), spec.get("openstack")
    profile = spec.get("profileRef", {})
    if (
        declaration.get("apiVersion") != f"{API_GROUP}/{API_VERSION}"
        or declaration.get("kind") != "ManagedCluster"
        or metadata.get("name") != policy.slug
        or metadata.get("namespace", expected_namespace) != expected_namespace
        or metadata.get("uid", policy.uid) != policy.uid
        or metadata.get("deletionTimestamp") is not None
        or spec.get("suspend", False) is not False
        or spec.get("deletionPolicy", "Retain") != "Retain"
        or not isinstance(dns, dict)
        or dns.get("apiHostname") != policy.parameters["apiHostname"]
        or dns.get("argocdHostname") != policy.parameters["argocdHostname"]
        or not isinstance(profile, dict)
        or profile.get("name", DEFAULT_PROFILE) != policy.parameters["profileName"]
        or not isinstance(project_spec, dict)
        or project_spec.get("projectName") != project_name
        or type(spec.get("workerGroups")) is not int
        or spec["workerGroups"] * 3 != workers
    ):
        raise InventoryConflict("ManagedCluster declaration no longer matches the publication job")
