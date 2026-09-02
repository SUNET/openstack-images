from copy import deepcopy

import pytest

from customer_cluster_operator.models import build_input


@pytest.fixture
def spec():
    return {
        "displayName": "Example Cluster",
        "contractNumber": "C-123",
        "customerDomain": "example.org",
        "workerGroups": 2,
        "openstack": {
            "projectName": "customer-example",
            "projectResourceName": "customer-example",
        },
        "dns": {"zone": "example.org"},
        "openbao": {"mount": "kubernetes/example"},
    }


@pytest.fixture
def profile():
    return {
        "projectNamespace": "customer-projects",
        "maxWorkerGroups": 80,
        "openstack": {
            "cloud": "production",
            "image": "Debian 13 Trixie",
            "externalNetwork": "public",
            "credentialsSecret": {"name": "clouds", "key": "clouds.yaml"},
            "controller": {"flavor": "b2.c4r8", "rootVolumeGB": 80},
            "worker": {"flavor": "b2.c8r16", "rootVolumeGB": 120},
            "jumphost": {"flavor": "b2.c1r2", "rootVolumeGB": 20},
        },
        "network": {
            "cidr": "10.44.0.0/24",
            "dnsNameservers": ["1.1.1.1", "9.9.9.9"],
            "apiVipAddress": "10.44.0.10",
            "ingressVipAddress": "10.44.0.11",
            "sshAllowedCIDRs": ["192.0.2.1/32"],
        },
        "ssh": {
            "authorizedKeysConfigMap": {
                "name": "cluster-authorized-keys",
                "key": "authorized_keys",
            }
        },
        "git": {
            "repoUrl": "https://git.example.org/clusters.git",
            "branch": "main",
            "username": "cluster-bot",
            "tokenSecret": {"name": "cluster-git", "key": "token"},
        },
    }


@pytest.fixture
def provisioning_input(spec, profile):
    return build_input(
        spec=deepcopy(spec),
        profile=deepcopy(profile),
        uid="12345678-1234-1234-1234-123456789abc",
        slug="example",
        namespace="openstack-operator",
        project_id="project-id",
        operator_namespace="openstack-operator",
    )


@pytest.fixture
def body(spec):
    return {
        "apiVersion": "customer-clusters.sunet.se/v1alpha1",
        "kind": "ManagedCluster",
        "metadata": {
            "name": "example",
            "namespace": "openstack-operator",
            "uid": "12345678-1234-1234-1234-123456789abc",
            "generation": 4,
        },
        "spec": deepcopy(spec),
    }
