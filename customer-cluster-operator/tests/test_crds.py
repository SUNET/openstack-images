"""Offline structural and admission constraints for the canonical CRDs."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

CRD_ROOT = Path(__file__).resolve().parents[1] / "crds"


def _walk_schema(value: object, location: str = "schema") -> Iterator[object]:
    if isinstance(value, dict):
        assert not ("properties" in value and "additionalProperties" in value), (
            f"{location} combines properties and additionalProperties"
        )
        for key, child in value.items():
            yield from _walk_schema(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_schema(child, f"{location}[{index}]")
    yield value


def _properties(filename: str) -> dict[str, Any]:
    document = yaml.safe_load((CRD_ROOT / filename).read_text(encoding="utf-8"))
    return document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]


def _string_accepts(schema: dict[str, Any], value: str) -> bool:
    """Check string constraints and the negated-match CEL subset used by these CRDs."""
    if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
        return False
    if not re.fullmatch(schema["pattern"], value):
        return False
    for validation in schema.get("x-kubernetes-validations", []):
        rule = re.fullmatch(r"!self\.matches\('(.+)'\)", validation["rule"])
        assert rule is not None, f"Unsupported offline CEL check: {validation['rule']}"
        if re.search(rule[1], value):
            return False
    return True


def test_canonical_crds_are_structural() -> None:
    expected = {
        "clusterprofile_crd.yaml": "clusterprofiles.customer-clusters.sunet.se",
        "managedcluster_crd.yaml": "managedclusters.customer-clusters.sunet.se",
    }

    for filename, name in expected.items():
        document = yaml.safe_load((CRD_ROOT / filename).read_text(encoding="utf-8"))
        assert document["kind"] == "CustomResourceDefinition"
        assert document["metadata"]["name"] == name
        list(_walk_schema(document["spec"]["versions"]))


def test_endpoint_fields_are_required_and_exposed_in_status() -> None:
    profile_spec = _properties("clusterprofile_crd.yaml")["spec"]
    network = profile_spec["properties"]["network"]
    assert {"apiVipAddress", "ingressVipAddress"} <= set(network["required"])

    status = _properties("managedcluster_crd.yaml")["status"]["properties"]
    assert status["apiFloatingIp"]["format"] == "ipv4"
    assert status["ingressFloatingIp"]["format"] == "ipv4"


def test_ansible_is_admission_optional_with_required_explicit_fields() -> None:
    spec = _properties("clusterprofile_crd.yaml")["spec"]
    ansible = spec["properties"]["ansible"]

    assert "ansible" not in spec["required"]
    assert ansible["type"] == "object"
    assert set(ansible["required"]) == {"nodeInterface", "pythonInterpreter"}
    assert all("default" not in item for item in _walk_schema(ansible) if isinstance(item, dict))


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("ens3", True),
        ("eth0.100", True),
        ("veth_A-1:2", True),
        ("0", True),
        ("a" * 15, True),
        ("a" * 16, False),
        ("", False),
        (".ens3", False),
        ("_ens3", False),
        ("-ens3", False),
        (" ens3", False),
        ("ens3 ", False),
        ("ens3\n", False),
        ("ens3/0", False),
        ("ens3;id", False),
        ("$(id)", False),
        ("ens\u00e9", False),
    ],
)
def test_node_interface_is_a_literal_linux_name(value: str, accepted: bool) -> None:
    field = _properties("clusterprofile_crd.yaml")["spec"]["properties"]["ansible"]["properties"][
        "nodeInterface"
    ]
    assert field["type"] == "string"
    assert field["maxLength"] == 15
    assert _string_accepts(field, value) is accepted


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("/usr/bin/python3", True),
        ("/usr/bin/python3.13", True),
        ("/python3", True),
        ("/opt/.venv+prod-x_y/bin/python3.0", True),
        ("/opt/a..b/bin/python3", True),
        ("/" + "a" * 503 + "/python3", True),
        ("/" + "a" * 504 + "/python3", False),
        ("", False),
        ("python3", False),
        ("usr/bin/python3", False),
        ("/usr/bin/python", False),
        ("/usr/bin/python2", False),
        ("/usr/bin/python3.", False),
        ("/usr/bin/python3.13.1", False),
        ("/usr/bin/python3.abc", False),
        ("//usr/bin/python3", False),
        ("/usr//bin/python3", False),
        ("/usr/./bin/python3", False),
        ("/usr/../bin/python3", False),
        ("/../python3", False),
        ("/./python3", False),
        ("/usr/bin/python3/", False),
        (" /usr/bin/python3", False),
        ("/usr/bin/python3\n", False),
        ("/usr/bin/python3 -I", False),
        ("/opt/my venv/bin/python3", False),
        ("/opt/\t/bin/python3", False),
        ("/opt/$(id)/python3", False),
        ("/opt/`id`/python3", False),
        ("/opt/a;b/python3", False),
        ("/opt/a&b/python3", False),
        ("/opt/a|b/python3", False),
        ("/opt/*/python3", False),
        ("/opt/~/python3", False),
        ("/opt/\u00e9/bin/python3", False),
    ],
)
def test_python_interpreter_is_a_normalized_absolute_python3_path(
    value: str,
    accepted: bool,
) -> None:
    field = _properties("clusterprofile_crd.yaml")["spec"]["properties"]["ansible"]["properties"][
        "pythonInterpreter"
    ]
    assert field["type"] == "string"
    assert field["maxLength"] == 512
    assert _string_accepts(field, value) is accepted


@pytest.mark.parametrize("field", ["apiHostname", "argocdHostname", "argocdAlias"])
def test_dns_names_are_admission_optional_lowercase_fqdns(field: str) -> None:
    spec = _properties("managedcluster_crd.yaml")["spec"]
    dns = spec["properties"]["dns"]
    hostname = dns["properties"][field]

    assert "dns" not in spec["required"]
    assert field not in dns.get("required", [])
    assert "default" not in dns
    assert "default" not in hostname
    assert hostname["type"] == "string"
    assert hostname["maxLength"] == 253
    assert _string_accepts(hostname, "argocd.example.org")
    assert _string_accepts(hostname, ".".join(["a" * 63] * 3 + ["b" * 61]))

    invalid_names = [
        "",
        "localhost",
        "Argocd.example.org",
        "argocd.example.org.",
        "https://argocd.example.org",
        "argocd.example.org:443",
        "argocd.example.org/path",
        "*.example.org",
        "argocd_alias.example.org",
        "argocd..example.org",
        "-argocd.example.org",
        "argocd-.example.org",
        "argocd.example.org\n",
        "192.0.2.1",
        "2001:db8::1",
        "a" * 64 + ".example.org",
        ".".join(["a" * 63] * 3 + ["b" * 62]),
    ]
    for value in invalid_names:
        assert not _string_accepts(hostname, value), value


def test_inventory_receipt_fields_are_optional_and_constrained() -> None:
    status = _properties("managedcluster_crd.yaml")["status"]
    properties = status["properties"]
    for field in ("inputHash", "inventoryInputHash", "publicationHash"):
        assert field not in status.get("required", [])
        assert properties[field]["type"] == "string"
        assert "default" not in properties[field]
        assert _string_accepts(properties[field], "0123456789abcdef" * 4)
        for value in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
            assert not _string_accepts(properties[field], value)

    assert "policyInventoryPath" not in status.get("required", [])
    policy = properties["policyInventoryPath"]
    assert policy["type"] == "string"
    assert "default" not in policy
    for slug in ("a", "eosc-one", "a" * 63):
        assert _string_accepts(policy, f"inventory/clusters/{slug}.yml")
    for value in (
        "inventory/clusters/.yml",
        "inventory/clusters/../eosc-one.yml",
        "inventory/clusters/EOSC-one.yml",
        "inventory/clusters/-eosc-one.yml",
        "inventory/clusters/eosc-one-.yml",
        "inventory/clusters/" + "a" * 64 + ".yml",
        "inventory/clusters/eosc-one.yaml",
        "/inventory/clusters/eosc-one.yml",
        "clusters/eosc-one/generated/ansible/hosts.yml",
    ):
        assert not _string_accepts(policy, value), value
