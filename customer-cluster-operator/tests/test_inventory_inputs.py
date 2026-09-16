from copy import deepcopy

import pytest

from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.inventory_inputs import (
    build_inventory_parameters,
    validate_inventory_parameters,
    validate_profile_revision,
)

INVENTORY_FIELDS = (
    "profileName",
    "apiHostname",
    "argocdHostname",
    "nodeInterface",
    "pythonInterpreter",
)


@pytest.fixture
def inventory_parameters():
    return {
        "profileName": "standard-v1",
        "apiHostname": "api.example.example.org",
        "argocdHostname": "argocd.example.example.org",
        "nodeInterface": "ens3",
        "pythonInterpreter": "/usr/bin/python3",
    }


def test_validate_returns_only_the_five_fields_without_mutating_input(inventory_parameters):
    original = deepcopy(inventory_parameters)
    result = validate_inventory_parameters(inventory_parameters)
    assert result == original
    assert inventory_parameters == original
    result["nodeInterface"] = "enp1s0"
    assert inventory_parameters == original


@pytest.mark.parametrize("value", [None, False, 1, "inventory", [], (), [("profileName", "v1")]])
def test_inventory_must_be_an_object(value):
    with pytest.raises(ValidationError, match="inventory"):
        validate_inventory_parameters(value)


@pytest.mark.parametrize("field", INVENTORY_FIELDS)
def test_all_five_inventory_fields_are_required(inventory_parameters, field):
    inventory_parameters.pop(field)
    with pytest.raises(ValidationError, match="inventory.*required"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize(
    "field",
    [
        "extraVars",
        "token",
        "password",
        "cloudsYaml",
        "credentialsSecret",
        "ansible_ssh_private_key_file",
        "ansible_connection",
        "argocdAlias",
        "zone",
    ],
)
def test_inventory_rejects_additional_fields_including_credentials(inventory_parameters, field):
    inventory_parameters[field] = "unreviewed-value"
    with pytest.raises(ValidationError, match="inventory"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize("field", INVENTORY_FIELDS)
@pytest.mark.parametrize("value", [None, True, 1, 1.5, [], {}, b"text"])
def test_inventory_fields_require_strings(inventory_parameters, field, value):
    inventory_parameters[field] = value
    with pytest.raises(ValidationError, match=rf"inventory\.{field}.*string"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize("field", INVENTORY_FIELDS)
@pytest.mark.parametrize("value", ["", " ", "\t", "\n", " value", "value ", "value\n"])
def test_inventory_fields_reject_empty_or_padded_values(inventory_parameters, field, value):
    inventory_parameters[field] = value
    with pytest.raises(ValidationError, match=rf"inventory\.{field}"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize("value", ["standard-v1", "reviewed-v2", "a", "a" * 63])
def test_profile_name_accepts_dns_labels(inventory_parameters, value):
    inventory_parameters["profileName"] = value
    assert validate_inventory_parameters(inventory_parameters)["profileName"] == value


@pytest.mark.parametrize(
    "value",
    ["a" * 64, "Uppercase", "with_underscore", "a.b", "-a", "a-", "../profile", "{{ profile }}"],
)
def test_profile_name_rejects_invalid_labels(inventory_parameters, value):
    inventory_parameters["profileName"] = value
    with pytest.raises(ValidationError, match=r"inventory\.profileName"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize("field", ["apiHostname", "argocdHostname"])
@pytest.mark.parametrize(
    "value",
    [
        "api.tenant.example.org",
        "argocd-1.tenant.example.org",
        "api.192.0.2.1.example.org",
        "xn--bcher-kva.example.org",
        pytest.param("a" * 63 + ".example.org", id="max-label-length"),
        pytest.param(".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61]), id="max-fqdn-length"),
    ],
)
def test_hostnames_accept_literal_fqdns_and_length_boundaries(inventory_parameters, field, value):
    inventory_parameters[field] = value
    assert validate_inventory_parameters(inventory_parameters)[field] == value


@pytest.mark.parametrize("field", ["apiHostname", "argocdHostname"])
@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "api.example.org.",
        ".example.org",
        "api..example.org",
        "-api.example.org",
        "api-.example.org",
        "api_node.example.org",
        "API.example.org",
        "api.exämple.org",
        "127.0.0.1",
        "192.000.2.1",
        "999.1.2.3",
        "2001:db8::1",
        "https://api.example.org",
        "api.example.org:6443",
        "api.example.org/path",
        "*.example.org",
        "api.example.org;id",
        "$(id).example.org",
        "`id`.example.org",
        "{{ api_hostname }}",
        "api.example.org\nansible_connection=local",
        "api.example.org\x00",
        pytest.param("a" * 64 + ".example.org", id="label-too-long"),
        pytest.param(".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62]), id="fqdn-too-long"),
    ],
)
def test_hostnames_reject_noncanonical_names_and_injection(inventory_parameters, field, value):
    inventory_parameters[field] = value
    with pytest.raises(ValidationError, match=rf"inventory\.{field}"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize(
    "value", ["ens3", "enp1s0", "bond0.123", "eth0:1", "br-ex", "veth_1", "a" * 15],
)
def test_interface_accepts_literal_linux_names(inventory_parameters, value):
    inventory_parameters["nodeInterface"] = value
    assert validate_inventory_parameters(inventory_parameters)["nodeInterface"] == value


@pytest.mark.parametrize(
    "value",
    [
        "a" * 16,
        "-ens3",
        ".ens3",
        "ens3@if4",
        "ens3/other",
        "ens3 other",
        "ens3;id",
        "ens3$(id)",
        "ens3`id`",
        "{{ interface }}",
        "ens3\nlocal",
        "ens3\x00",
    ],
)
def test_interface_rejects_long_names_and_injection(inventory_parameters, value):
    inventory_parameters["nodeInterface"] = value
    with pytest.raises(ValidationError, match=r"inventory\.nodeInterface"):
        validate_inventory_parameters(inventory_parameters)


@pytest.mark.parametrize(
    "value",
    [
        "/usr/bin/python3",
        "/usr/bin/python3.12",
        "/opt/python-3.13/bin/python3.13",
        "/opt/python_3+custom/bin/python3",
        pytest.param("/" + "a" * 252 + "/" + "b" * 250 + "/python3", id="max-path-length"),
    ],
)
def test_interpreter_accepts_absolute_python3_paths(inventory_parameters, value):
    inventory_parameters["pythonInterpreter"] = value
    assert validate_inventory_parameters(inventory_parameters)["pythonInterpreter"] == value


@pytest.mark.parametrize(
    "value",
    [
        "python3",
        "auto",
        "auto_silent",
        "~/bin/python3",
        "/usr/bin/python",
        "/usr/bin/python2",
        "/usr/bin/python3-config",
        "/bin/sh",
        "/usr/bin/python3 -I",
        "/usr/bin/python3;id",
        "/usr/bin/python3\nid",
        "/usr/$(id)/python3",
        "/usr/`id`/python3",
        "/{{ interpreter }}/python3",
        "/usr/bin/../bin/python3",
        "/usr/./bin/python3",
        "/usr//bin/python3",
        "//usr/bin/python3",
        "/usr/bin/python3/",
        "/usr/bin/python3\x00",
        pytest.param("/" + "a" * 252 + "/" + "b" * 251 + "/python3", id="path-too-long"),
    ],
)
def test_interpreter_rejects_discovery_relative_paths_and_injection(inventory_parameters, value):
    inventory_parameters["pythonInterpreter"] = value
    with pytest.raises(ValidationError, match=r"inventory\.pythonInterpreter"):
        validate_inventory_parameters(inventory_parameters)


def test_builder_uses_canonical_dns_and_reviewed_profile_ansible(spec, profile):
    spec["dns"].update(
        apiHostname="api.canonical.example.net",
        argocdHostname="argocd.canonical.example.net",
        zone="unrelated.example.org",
        argocdAlias="alias.unrelated.example.org",
    )
    spec["ansible"] = {"nodeInterface": "unreviewed0", "pythonInterpreter": "/bin/sh"}
    spec["inventory"] = {"profileName": "unreviewed-v1", "ansible_connection": "local"}
    profile["ansible"] = {
        "nodeInterface": "enp1s0",
        "pythonInterpreter": "/opt/python/bin/python3.13",
        "extraVars": {"ansible_password": "excluded-password"},
    }
    original_spec = deepcopy(spec)
    original_profile = deepcopy(profile)

    result = build_inventory_parameters(spec, profile, "reviewed-v2")

    assert result == {
        "profileName": "reviewed-v2",
        "apiHostname": "api.canonical.example.net",
        "argocdHostname": "argocd.canonical.example.net",
        "nodeInterface": "enp1s0",
        "pythonInterpreter": "/opt/python/bin/python3.13",
    }
    assert spec == original_spec
    assert profile == original_profile


@pytest.mark.parametrize(
    ("owner", "section", "message"),
    [("spec", "dns", r"spec\.dns"), ("profile", "ansible", r"profile\.spec\.ansible")],
)
def test_builder_requires_dns_and_ansible_sections(spec, profile, owner, section, message):
    source = spec if owner == "spec" else profile
    source.pop(section)
    with pytest.raises(ValidationError, match=message):
        build_inventory_parameters(spec, profile, "standard-v1")


@pytest.mark.parametrize(
    ("owner", "section", "message"),
    [("spec", "dns", r"spec\.dns"), ("profile", "ansible", r"profile\.spec\.ansible")],
)
@pytest.mark.parametrize("value", [None, False, 1, "configured", []])
def test_builder_requires_object_sections(spec, profile, owner, section, message, value):
    source = spec if owner == "spec" else profile
    source[section] = value
    with pytest.raises(ValidationError, match=message):
        build_inventory_parameters(spec, profile, "standard-v1")


@pytest.mark.parametrize("field", ["apiHostname", "argocdHostname"])
def test_builder_never_invents_canonical_hostnames_from_zone_or_alias(spec, profile, field):
    spec["dns"]["zone"] = "example.org"
    spec["dns"]["argocdAlias"] = "alias.example.org"
    spec["dns"].pop(field)
    with pytest.raises(ValidationError, match=rf"inventory\.{field}"):
        build_inventory_parameters(spec, profile, "standard-v1")


@pytest.mark.parametrize("field", ["nodeInterface", "pythonInterpreter"])
def test_builder_never_defaults_missing_reviewed_ansible_fields(spec, profile, field):
    spec["ansible"] = deepcopy(profile["ansible"])
    profile["ansible"].pop(field)
    with pytest.raises(ValidationError, match=rf"inventory\.{field}"):
        build_inventory_parameters(spec, profile, "standard-v1")


@pytest.mark.parametrize("generation", [1, 2, 123])
def test_profile_revision_preserves_uid_and_integer_generation(generation):
    revision = {"uid": "profile-uid", "generation": generation}
    original = deepcopy(revision)
    result = validate_profile_revision(revision)

    assert result == original
    assert isinstance(result["uid"], str)
    assert type(result["generation"]) is int
    assert revision == original
    result.update(uid="recreated-profile-uid", generation=generation + 1)
    assert revision == original


@pytest.mark.parametrize("revision", [None, False, 1, "profile-uid", [], ()])
def test_profile_revision_requires_an_object(revision):
    with pytest.raises(ValidationError, match="profileRevision"):
        validate_profile_revision(revision)


@pytest.mark.parametrize("field", ["uid", "generation"])
def test_profile_revision_requires_both_metadata_fields(field):
    revision = {"uid": "profile-uid", "generation": 1}
    revision.pop(field)
    with pytest.raises(ValidationError, match="profileRevision"):
        validate_profile_revision(revision)


@pytest.mark.parametrize(
    "uid",
    [None, True, 1, [], {}, b"profile-uid", "", " ", " profile-uid", "profile-uid "],
)
def test_profile_revision_requires_a_nonempty_string_uid(uid):
    with pytest.raises(ValidationError, match="uid"):
        validate_profile_revision({"uid": uid, "generation": 1})


@pytest.mark.parametrize("generation", [None, True, False, 0, -1, "", "1", 1.0, [], {}])
def test_profile_revision_requires_a_positive_integer_generation(generation):
    with pytest.raises(ValidationError, match="generation"):
        validate_profile_revision({"uid": "profile-uid", "generation": generation})
