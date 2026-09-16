import hashlib
import json
from copy import deepcopy

import pytest

from customer_cluster_operator.constants import DEFAULT_PROFILE
from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.models import (
    ProvisioningInput,
    build_input,
    is_suspended,
    job_name,
    profile_name,
)

GOLDEN_V1_HASH = "e2a279ed8c80059d96a33fffbd9e90841c45a736c8fb28dd58895897d2db285e"
GOLDEN_V1_JSON = (
    '{"cluster":{"slug":"example","uid":"12345678-1234-1234-1234-123456789abc"},'
    '"git":{"branch":"main","repoUrl":"https://git.example.org/clusters.git",'
    '"tokenSecret":{"key":"token","name":"cluster-git","namespace":"openstack-operator"},'
    '"username":"cluster-bot"},'
    '"network":{"apiVipAddress":"10.44.0.10","cidr":"10.44.0.0/24",'
    '"dnsNameservers":["1.1.1.1","9.9.9.9"],"ingressVipAddress":"10.44.0.11",'
    '"sshAllowedCIDRs":["192.0.2.1/32"]},"nodes":{"controllers":3,"workers":6},'
    '"openstack":{"cloud":"production","controller":{"flavor":"b2.c4r8","rootVolumeGB":80},'
    '"credentialsSecret":{"key":"clouds.yaml","name":"clouds","namespace":"openstack-operator"},'
    '"externalNetwork":"public","image":"Debian 13 Trixie",'
    '"jumphost":{"flavor":"b2.c1r2","rootVolumeGB":20},'
    '"worker":{"flavor":"b2.c8r16","rootVolumeGB":120}},'
    '"project":{"id":"project-id","name":"customer-example"},"schemaVersion":1,'
    '"ssh":{"authorizedKeysConfigMap":{"key":"authorized_keys",'
    '"name":"cluster-authorized-keys","namespace":"openstack-operator"}}}'
)


def make_input(spec, profile, **overrides):
    values = {
        "spec": spec,
        "profile": profile,
        "profile_revision": {"uid": "profile-uid", "generation": 1},
        "uid": "12345678-1234-1234-1234-123456789abc",
        "slug": "example",
        "namespace": "openstack-operator",
        "project_id": "project-id",
        "operator_namespace": "openstack-operator",
    }
    values.update(overrides)
    return build_input(**values)


def test_defaults_profile_name(spec):
    assert profile_name(spec) == DEFAULT_PROFILE == "standard-v1"
    spec["profileRef"] = {"name": "large-v1"}
    assert profile_name(spec) == "large-v1"


@pytest.mark.parametrize("profile_ref", [{}, {"name": "standard-v1"}, {"name": "large-v1"}])
def test_inventory_uses_selected_or_default_profile_name(spec, profile, profile_ref):
    spec["profileRef"] = profile_ref
    result = make_input(spec, profile)
    assert result.data["inventory"]["profileName"] == profile_ref.get("name", "standard-v1")


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


def test_schema_v2_contains_legacy_data_inventory_and_profile_revision(spec, profile):
    result = make_input(spec, profile)
    expected = json.loads(GOLDEN_V1_JSON)
    expected.update(
        schemaVersion=2,
        inventory={
            "profileName": "standard-v1",
            "apiHostname": "api.example.example.org",
            "argocdHostname": "argocd.example.example.org",
            "nodeInterface": "ens3",
            "pythonInterpreter": "/usr/bin/python3",
        },
        profileRevision={"uid": "profile-uid", "generation": 1},
    )
    assert result.data == expected
    assert result.canonical_json == json.dumps(expected, sort_keys=True, separators=(",", ":"))
    assert json.loads(result.canonical_json) == expected


def test_schema_upgrade_and_publication_metadata_preserve_golden_v1_hash(spec, profile):
    legacy = ProvisioningInput(json.loads(GOLDEN_V1_JSON))
    upgraded = make_input(spec, profile)
    original_data = deepcopy(upgraded.data)

    assert legacy.canonical_json == GOLDEN_V1_JSON
    assert hashlib.sha256(GOLDEN_V1_JSON.encode("utf-8")).hexdigest() == GOLDEN_V1_HASH
    assert legacy.input_hash == upgraded.input_hash == GOLDEN_V1_HASH
    assert legacy.publication_hash == GOLDEN_V1_HASH
    assert upgraded.publication_hash != GOLDEN_V1_HASH
    assert upgraded.data == original_data
    assert legacy.data == json.loads(GOLDEN_V1_JSON)


def test_publication_hash_covers_full_worker_json_and_inventory_hash_only_inventory(
    provisioning_input,
):
    result = provisioning_input
    worker_json = json.dumps(result.data, sort_keys=True, separators=(",", ":"))
    inventory_json = json.dumps(result.data["inventory"], sort_keys=True, separators=(",", ":"))
    assert result.canonical_json == worker_json
    assert result.publication_hash == hashlib.sha256(worker_json.encode("utf-8")).hexdigest()
    assert result.inventory_hash == hashlib.sha256(inventory_json.encode("utf-8")).hexdigest()
    assert len({result.input_hash, result.publication_hash, result.inventory_hash}) == 3


def test_all_hashes_are_independent_of_mapping_order(provisioning_input):
    reordered = ProvisioningInput(
        json.loads(
            provisioning_input.canonical_json,
            object_pairs_hook=lambda items: dict(reversed(items)),
        )
    )
    assert reordered.canonical_json == provisioning_input.canonical_json
    assert reordered.input_hash == provisioning_input.input_hash == GOLDEN_V1_HASH
    assert reordered.publication_hash == provisioning_input.publication_hash
    assert reordered.inventory_hash == provisioning_input.inventory_hash


def test_hashes_ignore_irrelevant_fields_when_canonical_hostnames_are_unchanged(spec, profile):
    first = make_input(deepcopy(spec), deepcopy(profile))
    spec["displayName"] = "Renamed"
    spec["dns"].update(zone="changed.example", argocdAlias="argocd.example.org")
    spec["openbao"] = {"mount": "changed"}
    second = make_input(spec, profile)
    assert first.input_hash == second.input_hash == GOLDEN_V1_HASH
    assert first.inventory_hash == second.inventory_hash
    assert first.publication_hash == second.publication_hash
    assert first.canonical_json == second.canonical_json
    assert first.data == second.data


@pytest.mark.parametrize(
    ("mutation", "field", "value"),
    [
        pytest.param(
            lambda s, p: s["dns"].update(apiHostname="api.changed.example.org"),
            "apiHostname",
            "api.changed.example.org",
            id="api-hostname",
        ),
        pytest.param(
            lambda s, p: s["dns"].update(argocdHostname="argocd.changed.example.org"),
            "argocdHostname",
            "argocd.changed.example.org",
            id="argocd-hostname",
        ),
        pytest.param(
            lambda s, p: p["ansible"].update(nodeInterface="enp1s0"),
            "nodeInterface",
            "enp1s0",
            id="interface",
        ),
        pytest.param(
            lambda s, p: p["ansible"].update(pythonInterpreter="/opt/python/bin/python3.13"),
            "pythonInterpreter",
            "/opt/python/bin/python3.13",
            id="interpreter",
        ),
        pytest.param(
            lambda s, p: s.update(profileRef={"name": "reviewed-v2"}),
            "profileName",
            "reviewed-v2",
            id="profile-name",
        ),
    ],
)
def test_policy_changes_only_inventory_and_publication_hashes(
    spec, profile, mutation, field, value,
):
    first = make_input(spec, profile)
    mutation(spec, profile)
    second = make_input(spec, profile)
    expected_inventory = first.data["inventory"] | {field: value}

    assert second.data == first.data | {"inventory": expected_inventory}
    assert second.input_hash == first.input_hash == GOLDEN_V1_HASH
    assert second.inventory_hash != first.inventory_hash
    assert second.publication_hash != first.publication_hash
    assert second.canonical_json != first.canonical_json


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param({"uid": "profile-uid", "generation": 2}, id="new-generation"),
        pytest.param({"uid": "recreated-profile-uid", "generation": 1}, id="new-uid"),
    ],
)
def test_profile_revision_changes_only_publication_identity(spec, profile, revision):
    first = make_input(spec, profile)
    second = make_input(spec, profile, profile_revision=revision)

    assert second.data == first.data | {"profileRevision": revision}
    assert first.data["profileRevision"] == {"uid": "profile-uid", "generation": 1}
    assert second.input_hash == first.input_hash == GOLDEN_V1_HASH
    assert second.inventory_hash == first.inventory_hash
    assert second.publication_hash != first.publication_hash
    assert second.canonical_json != first.canonical_json


def test_profile_rollback_has_fresh_publication_identity_with_original_inventory(spec, profile):
    original_profile = deepcopy(profile)
    first = make_input(spec, profile)
    profile["ansible"]["nodeInterface"] = "enp1s0"
    changed = make_input(
        spec, profile, profile_revision={"uid": "profile-uid", "generation": 2},
    )
    reverted = make_input(
        spec, original_profile, profile_revision={"uid": "profile-uid", "generation": 3},
    )

    assert first.input_hash == changed.input_hash == reverted.input_hash == GOLDEN_V1_HASH
    assert first.inventory_hash == reverted.inventory_hash != changed.inventory_hash
    assert reverted.data == first.data | {
        "profileRevision": {"uid": "profile-uid", "generation": 3},
    }
    assert len({first.publication_hash, changed.publication_hash, reverted.publication_hash}) == 3


def test_profile_revision_is_a_required_keyword(spec, profile):
    with pytest.raises(TypeError, match="profile_revision"):
        build_input(
            spec=spec,
            profile=profile,
            uid="12345678-1234-1234-1234-123456789abc",
            slug="example",
            namespace="openstack-operator",
            project_id="project-id",
            operator_namespace="openstack-operator",
        )


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param(None, id="null-revision"),
        pytest.param({}, id="empty-revision"),
        pytest.param({"generation": 1}, id="missing-uid"),
        pytest.param({"uid": None, "generation": 1}, id="null-uid"),
        pytest.param({"uid": "", "generation": 1}, id="empty-uid"),
        pytest.param({"uid": "profile-uid"}, id="missing-generation"),
        pytest.param({"uid": "profile-uid", "generation": None}, id="null-generation"),
        pytest.param({"uid": "profile-uid", "generation": True}, id="boolean-generation"),
        pytest.param({"uid": "profile-uid", "generation": 0}, id="zero-generation"),
        pytest.param({"uid": "profile-uid", "generation": -1}, id="negative-generation"),
        pytest.param({"uid": "profile-uid", "generation": ""}, id="empty-generation"),
        pytest.param({"uid": "profile-uid", "generation": "1"}, id="string-generation"),
    ],
)
def test_invalid_profile_revision_is_rejected_while_building_worker_input(spec, profile, revision):
    with pytest.raises(ValidationError, match="profileRevision"):
        make_input(spec, profile, profile_revision=revision)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda s, p: s.update(workerGroups=3), id="worker-groups"),
        pytest.param(lambda s, p: p["network"].update(cidr="10.44.0.0/23"), id="cidr"),
        pytest.param(
            lambda s, p: p["openstack"]["controller"].update(flavor="b2.c8r16"),
            id="controller-flavor",
        ),
        pytest.param(
            lambda s, p: p["openstack"]["worker"].update(flavor="b2.c16r32"),
            id="worker-flavor",
        ),
        pytest.param(
            lambda s, p: p["openstack"]["jumphost"].update(flavor="b2.c2r4"),
            id="jumphost-flavor",
        ),
        pytest.param(
            lambda s, p: p["openstack"]["worker"].update(rootVolumeGB=121),
            id="root-volume",
        ),
    ],
)
def test_infrastructure_changes_preserve_v1_hash_guard(spec, profile, mutation):
    first = make_input(spec, profile)
    mutation(spec, profile)
    second = make_input(spec, profile)
    legacy_data = deepcopy(second.data)
    legacy_data.pop("inventory")
    legacy_data.pop("profileRevision")
    legacy_data["schemaVersion"] = 1
    legacy_json = json.dumps(legacy_data, sort_keys=True, separators=(",", ":"))

    assert first.input_hash == GOLDEN_V1_HASH
    assert second.input_hash != first.input_hash
    assert second.input_hash == hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
    assert second.publication_hash != first.publication_hash
    assert second.inventory_hash == first.inventory_hash
    assert second.data["inventory"] == first.data["inventory"]


def test_worker_envelope_excludes_credentials_and_unreviewed_fields(spec, profile):
    first = make_input(spec, profile)
    credentials = {
        "password": "excluded-password-marker",
        "token": "excluded-token-marker",
        "cloudsYaml": "excluded-clouds-marker",
        "privateKey": "excluded-private-key-marker",
    }
    for source in (
        spec,
        spec["dns"],
        spec["openstack"],
        profile,
        profile["ansible"],
        profile["openstack"],
        profile["openstack"]["controller"],
        profile["openstack"]["worker"],
        profile["openstack"]["jumphost"],
        profile["openstack"]["credentialsSecret"],
        profile["network"],
        profile["ssh"],
        profile["ssh"]["authorizedKeysConfigMap"],
        profile["git"],
        profile["git"]["tokenSecret"],
    ):
        source.update(credentials)
    spec["inventory"] = {"ansible_connection": "local"}
    spec["profileRevision"] = {"uid": "unreviewed-profile-uid", "generation": 99}
    profile["profileRevision"] = {"uid": "unreviewed-profile-uid", "generation": 99}
    spec["ansible"] = {"nodeInterface": "unreviewed0", "pythonInterpreter": "/bin/sh"}
    profile["ansible"]["extraVars"] = {"ansible_password": "excluded-ansible-marker"}

    result = make_input(spec, profile)
    assert result.data == first.data
    assert result.canonical_json == first.canonical_json
    assert "excluded-" not in result.canonical_json
    assert result.input_hash == GOLDEN_V1_HASH


def test_input_is_a_snapshot_and_does_not_mutate_api_objects(spec, profile):
    original_spec = deepcopy(spec)
    original_profile = deepcopy(profile)
    revision = {"uid": "profile-uid", "generation": 1}
    result = make_input(spec, profile, profile_revision=revision)
    assert spec == original_spec
    assert profile == original_profile
    assert revision == {"uid": "profile-uid", "generation": 1}
    expected_data = deepcopy(result.data)
    expected_json = result.canonical_json

    spec["dns"]["apiHostname"] = "api.changed.example.org"
    profile["ansible"]["nodeInterface"] = "enp1s0"
    profile["network"]["dnsNameservers"].append("192.0.2.53")
    profile["git"]["tokenSecret"]["name"] = "changed-token"
    revision.update(uid="recreated-profile-uid", generation=2)

    assert result.data == expected_data
    assert result.canonical_json == expected_json
    assert result.input_hash == GOLDEN_V1_HASH


def test_inventory_publication_paths(provisioning_input):
    assert provisioning_input.inventory_path == "clusters/example/generated/ansible/hosts.yml"
    assert provisioning_input.policy_inventory_path == "inventory/clusters/example.yml"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s, p: s.pop("dns"), r"spec\.dns"),
        (lambda s, p: s["dns"].pop("apiHostname"), r"inventory\.apiHostname"),
        (lambda s, p: s["dns"].pop("argocdHostname"), r"inventory\.argocdHostname"),
        (lambda s, p: p.pop("ansible"), r"profile\.spec\.ansible"),
        (lambda s, p: p["ansible"].pop("nodeInterface"), r"inventory\.nodeInterface"),
        (lambda s, p: p["ansible"].pop("pythonInterpreter"), r"inventory\.pythonInterpreter"),
        (
            lambda s, p: s["dns"].update(apiHostname="api.example.org;id"),
            r"inventory\.apiHostname",
        ),
        (
            lambda s, p: s["dns"].update(argocdHostname="{{ argocd_alias }}"),
            r"inventory\.argocdHostname",
        ),
        (
            lambda s, p: s["dns"].update(apiHostname="192.000.2.1"),
            r"inventory\.apiHostname",
        ),
        (
            lambda s, p: s["dns"].update(argocdHostname="999.1.2.3"),
            r"inventory\.argocdHostname",
        ),
        (
            lambda s, p: p["ansible"].update(nodeInterface="ens3;id"),
            r"inventory\.nodeInterface",
        ),
        (
            lambda s, p: p["ansible"].update(pythonInterpreter="/bin/sh"),
            r"inventory\.pythonInterpreter",
        ),
        (
            lambda s, p: p["ansible"].update(pythonInterpreter="//usr/bin/python3"),
            r"inventory\.pythonInterpreter",
        ),
        (lambda s, p: s.update(profileRef={"name": "../profile"}), r"inventory\.profileName"),
    ],
)
def test_invalid_inventory_is_rejected_while_building_worker_input(
    spec, profile, mutation, message,
):
    mutation(spec, profile)
    with pytest.raises(ValidationError, match=message):
        make_input(spec, profile)


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
