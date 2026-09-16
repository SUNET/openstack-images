"""Kubespray inventory rendering and authenticated Git publication."""

from __future__ import annotations

from typing import Any

import yaml

from .publication import publish_inventory as publish_inventory
from .publication_policy import render_cluster_policy as render_cluster_policy


def inventory_document(resources: dict[str, Any]) -> dict[str, Any]:
    jump_ip = resources["jumphost"]["floating_ip"]
    ssh_common = f"-o StrictHostKeyChecking=accept-new -o ProxyJump=root@{jump_ip}"
    hosts = {
        node["name"]: {
            "ansible_host": node["ip"],
            "ip": node["ip"],
            "access_ip": node["ip"],
            "ansible_user": "root",
            "ansible_ssh_common_args": ssh_common,
        }
        for node in resources["controllers"] + resources["workers"]
    }
    controllers = {node["name"]: {} for node in resources["controllers"]}
    nodes = {node["name"]: {} for node in resources["workers"]}
    return {
        "all": {
            "vars": {
                "customer_cluster_api_vip": resources["api_vip"],
                "customer_cluster_ingress_vip": resources["ingress_vip"],
                "customer_cluster_api_floating_ip": resources["api_floating_ip"],
                "customer_cluster_ingress_floating_ip": resources["ingress_floating_ip"],
            },
            "hosts": hosts,
            "children": {
                "kube_control_plane": {"hosts": controllers},
                "kube_node": {"hosts": nodes},
                "etcd": {"hosts": controllers},
                "k8s_cluster": {
                    "children": {
                        "kube_control_plane": {},
                        "kube_node": {},
                    }
                },
                "calico_rr": {"hosts": {}},
            },
        }
    }


def render_inventory(resources: dict[str, Any]) -> str:
    return (
        "---\n"
        "# GENERATED FILE: customer-cluster-operator. Manual edits will be overwritten.\n"
        + yaml.safe_dump(inventory_document(resources), sort_keys=False)
    )
