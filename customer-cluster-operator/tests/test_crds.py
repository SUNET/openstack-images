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
