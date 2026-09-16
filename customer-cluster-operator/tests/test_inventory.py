from copy import deepcopy

import pytest
import yaml

from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.inventory import (
    inventory_document,
    render_cluster_policy,
    render_inventory,
)


@pytest.fixture
def resources():
    return {
        "jumphost": {"name": "jump", "floating_ip": "192.0.2.10"},
        "controllers": [
            {"name": "controller-01", "ip": "10.0.0.10"},
            {"name": "controller-02", "ip": "10.0.0.11"},
            {"name": "controller-03", "ip": "10.0.0.12"},
        ],
        "workers": [{"name": "worker-01", "ip": "10.0.0.20"}],
        "api_vip": "10.0.0.5",
        "ingress_vip": "10.0.0.6",
        "api_floating_ip": "192.0.2.11",
        "ingress_floating_ip": "192.0.2.12",
    }


def test_inventory_has_standard_groups_and_proxyjump(resources):
    document = inventory_document(resources)
    all_group = document["all"]
    assert set(all_group["children"]) == {
        "kube_control_plane",
        "kube_node",
        "etcd",
        "k8s_cluster",
        "calico_rr",
    }
    host = all_group["hosts"]["controller-01"]
    assert host["ansible_user"] == "root"
    assert host["ansible_host"] == host["access_ip"] == "10.0.0.10"
    assert "ProxyJump=root@192.0.2.10" in host["ansible_ssh_common_args"]
    assert set(all_group["children"]["kube_node"]["hosts"]) == {"worker-01"}
    assert set(all_group["children"]["k8s_cluster"]["children"]) == {
        "kube_control_plane",
        "kube_node",
    }
    assert all_group["vars"] == {
        "customer_cluster_api_vip": "10.0.0.5",
        "customer_cluster_ingress_vip": "10.0.0.6",
        "customer_cluster_api_floating_ip": "192.0.2.11",
        "customer_cluster_ingress_floating_ip": "192.0.2.12",
    }


def test_rendered_inventory_has_warning_and_no_credentials(resources):
    rendered = render_inventory(resources)
    assert "GENERATED FILE" in rendered
    assert "token" not in rendered.lower()
    assert yaml.safe_load(rendered)["all"]["hosts"]


def test_policy_uses_only_reviewed_metadata(provisioning_input):
    data = deepcopy(provisioning_input.data)
    data["credentials"] = {"token": "do-not-export"}
    data["dns"] = {"apiAlias": "alias.example.org"}
    rendered = render_cluster_policy(data)
    assert yaml.safe_load(rendered) == {"all": {"vars": {
        "customer_cluster_name": "example",
        "customer_cluster_profile": "standard-v1",
        "customer_cluster_node_interface": "ens3",
        "customer_cluster_api_hostname": "api.example.example.org",
        "customer_cluster_argocd_hostname": "argocd.example.example.org",
        "ansible_python_interpreter": "/usr/bin/python3",
    }}}
    assert "# GENERATED FILE: customer-cluster-operator" in rendered
    assert "# owner: customer-cluster-operator" in rendered
    assert "# formatVersion: 1" in rendered
    assert f"# ManagedClusterUID: {data['cluster']['uid']}" in rendered
    assert "do-not-export" not in rendered
    assert "alias.example.org" not in rendered
    assert "10.44." not in rendered
    assert "project-id" not in rendered


@pytest.mark.parametrize("key,value", [
    ("profileName", "../profile"),
    ("apiHostname", "192.0.2.1"),
    ("argocdHostname", "alias"),
    ("nodeInterface", "ens3; command"),
    ("pythonInterpreter", "auto"),
])
def test_policy_validates_inventory_inputs(provisioning_input, key, value):
    data = deepcopy(provisioning_input.data)
    data["inventory"][key] = value
    with pytest.raises(ValidationError):
        render_cluster_policy(data)


def test_policy_rejects_extra_fields_and_missing_canonical_dns(provisioning_input):
    data = deepcopy(provisioning_input.data)
    data["inventory"]["apiAlias"] = data["inventory"].pop("apiHostname")
    with pytest.raises(ValidationError):
        render_cluster_policy(data)
    data["inventory"]["apiHostname"] = "api.example.org"
    with pytest.raises(ValidationError):
        render_cluster_policy(data)
