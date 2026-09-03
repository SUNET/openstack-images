from copy import deepcopy

import pytest

from customer_cluster_operator.constants import DEFAULT_PROFILE
from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.models import build_input, is_suspended, job_name, profile_name


def make_input(spec, profile, **overrides):
    values = {
        "spec": spec,
        "profile": profile,
        "uid": "12345678-1234-1234-1234-123456789abc",
        "slug": "example",
        "namespace": "openstack-operator",
        "project_id": "project-id",
        "operator_namespace": "openstack-operator",
    }
    values.update(overrides)
    return build_input(**values)


def test_defaults_profile_name(spec):
    assert profile_name(spec) == DEFAULT_PROFILE
    spec["profileRef"] = {"name": "large-v1"}
    assert profile_name(spec) == "large-v1"


def test_suspend_defaults_false_and_supports_override(spec):
    assert is_suspended(spec) is False
    spec["suspend"] = True
    assert is_suspended(spec) is True


def test_worker_count_and_namespace_defaults(spec, profile):
    result = make_input(spec, profile)
    assert result.data["nodes"] == {"controllers": 3, "workers": 6}
    assert result.data["git"]["tokenSecret"]["namespace"] == "openstack-operator"


def test_worker_groups_must_not_exceed_profile_maximum(spec, profile):
    spec["workerGroups"] = 81
    with pytest.raises(ValidationError, match="maxWorkerGroups"):
        make_input(spec, profile)


def test_profile_maximum_must_fit_network_with_required_spares(spec, profile):
    profile["maxWorkerGroups"] = 40
    profile["network"]["cidr"] = "10.44.0.0/25"
    with pytest.raises(ValidationError, match="unsafe.*capacity"):
        make_input(spec, profile)

    profile["network"]["cidr"] = "10.44.0.0/24"
    make_input(spec, profile)


def test_hash_is_deterministic_and_ignores_irrelevant_fields(spec, profile):
    first = make_input(deepcopy(spec), deepcopy(profile))
    spec["displayName"] = "Renamed"
    spec["dns"] = {"zone": "changed.example"}
    spec["openbao"] = {"mount": "changed"}
    second = make_input(spec, profile)
    assert first.input_hash == second.input_hash
    assert first.canonical_json == second.canonical_json


def test_hash_changes_for_infrastructure(spec, profile):
    first = make_input(spec, deepcopy(profile))
    profile["openstack"]["worker"]["rootVolumeGB"] = 121
    assert first.input_hash != make_input(spec, profile).input_hash


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s, p: s.update(deletionPolicy="Delete"), "deletionPolicy"),
        (lambda s, p: s.update(workerGroups=0), "workerGroups"),
        (lambda s, p: p.pop("maxWorkerGroups"), "maxWorkerGroups"),
        (lambda s, p: p.update(maxWorkerGroups=81), "must not exceed 80"),
        (lambda s, p: p.update(projectNamespace=""), "projectNamespace"),
        (lambda s, p: s.update(suspend="false"), "suspend"),
        (
            lambda s, p: p["network"].update(cidr="10.44.0.1/24"),
            "valid network",
        ),
        (
            lambda s, p: p["network"].update(apiVipAddress="10.45.0.10"),
            "outside",
        ),
        (lambda s, p: p["network"].pop("apiVipAddress"), "apiVipAddress"),
        (
            lambda s, p: p["network"].update(apiVipAddress="10.44.0.0"),
            "network or broadcast address",
        ),
        (
            lambda s, p: p["network"].update(ingressVipAddress="10.44.0.255"),
            "network or broadcast address",
        ),
        (
            lambda s, p: p["network"].update(sshAllowedCIDRs=[]),
            "sshAllowedCIDRs",
        ),
        (
            lambda s, p: p["network"].update(sshAllowedCIDRs=["2001:db8::/64"]),
            "only IPv4",
        ),
        (
            lambda s, p: p["git"].update(repoUrl="http://git.example/repo"),
            "HTTPS",
        ),
        (
            lambda s, p: p["git"]["tokenSecret"].update(namespace="other"),
            "namespace",
        ),
    ],
)
def test_invalid_input_is_rejected(spec, profile, mutation, message):
    mutation(spec, profile)
    with pytest.raises(ValidationError, match=message):
        make_input(spec, profile)


def test_managed_cluster_must_use_operator_namespace(spec, profile):
    with pytest.raises(ValidationError, match="ManagedCluster namespace"):
        make_input(spec, profile, namespace="customer")


def test_job_name_is_stable_and_dns_length_bounded():
    value = job_name("a" * 63, "12345678-aaaa", "b" * 64)
    assert value == job_name("a" * 63, "12345678-aaaa", "b" * 64)
    assert len(value) <= 57
    assert len(f"{value}-input") <= 63
    assert value.endswith("-12345678-bbbbbbbbbb-g1-v0")
