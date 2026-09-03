import re
from pathlib import Path

import yaml

CRD_ROOT = Path(__file__).resolve().parents[1] / "crds"


def _walk_schema(value, location="schema"):
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


def test_canonical_crds_are_structural():
    expected = {
        "clusterprofile_crd.yaml": "clusterprofiles.customer-clusters.sunet.se",
        "managedcluster_crd.yaml": "managedclusters.customer-clusters.sunet.se",
    }

    for filename, name in expected.items():
        document = yaml.safe_load((CRD_ROOT / filename).read_text())
        assert document["kind"] == "CustomResourceDefinition"
        assert document["metadata"]["name"] == name
        list(_walk_schema(document["spec"]["versions"]))


def test_endpoint_fields_are_required_and_exposed_in_status():
    profile = yaml.safe_load((CRD_ROOT / "clusterprofile_crd.yaml").read_text())
    profile_spec = profile["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    network = profile_spec["properties"]["network"]
    assert {"apiVipAddress", "ingressVipAddress"} <= set(network["required"])

    managed = yaml.safe_load((CRD_ROOT / "managedcluster_crd.yaml").read_text())
    status = managed["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"][
        "properties"
    ]
    assert status["apiFloatingIp"]["format"] == "ipv4"
    assert status["ingressFloatingIp"]["format"] == "ipv4"


def test_argocd_alias_is_an_optional_lowercase_fqdn():
    managed = yaml.safe_load((CRD_ROOT / "managedcluster_crd.yaml").read_text())
    spec = managed["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    dns = spec["properties"]["dns"]
    alias = dns["properties"]["argocdAlias"]

    assert "argocdAlias" not in dns.get("required", [])
    assert alias["type"] == "string"
    assert alias["maxLength"] == 253

    validation = alias["x-kubernetes-validations"][0]
    ip_literal_pattern = r"^[0-9]+([.][0-9]+){3}$"
    assert validation["rule"] == f"!self.matches('{ip_literal_pattern}')"

    def schema_accepts(value):
        return bool(re.fullmatch(alias["pattern"], value)) and not bool(
            re.fullmatch(ip_literal_pattern, value)
        )

    assert schema_accepts("argocd.example.org")

    invalid_aliases = [
        "Argocd.example.org",
        "argocd.example.org.",
        "https://argocd.example.org",
        "argocd.example.org:443",
        "argocd.example.org/path",
        "*.example.org",
        "argocd_alias.example.org",
        "192.0.2.1",
        "2001:db8::1",
    ]
    for value in invalid_aliases:
        assert not schema_accepts(value)
