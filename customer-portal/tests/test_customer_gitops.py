"""Tests for credential-free customer GitOps rendering."""

import pytest

from app.customer_gitops import CustomerGitOpsError, render_tree


def test_render_tree_contains_only_customer_specific_values() -> None:
    tree = render_tree(
        repo_url="https://platform.sunet.se/VDC/customer-acme-clusters-test.git",
        slug="acme-one",
        hostname="argocd.acme-one.k8s-test.sunetvdc.se",
        ingress_vip="10.42.0.240",
        interface="ens3",
        acme_contact="noc@sunet.se",
    )

    assert "clusters/acme-one/addons/argocd-ingress/routes.yaml" in tree
    assert "clusters/acme-one/argocd-apps/customer-cluster-apps.yaml" in tree
    assert "https://platform.sunet.se/VDC/customer-acme-clusters-test.git" in (
        tree["clusters/acme-one/argocd-apps/argocd.yaml"]
    )
    assert "10.42.0.240" in tree["clusters/acme-one/addons/argocd-ingress/envoy-proxy.yaml"]
    assert "argocd.acme-one.k8s-test.sunetvdc.se" in (
        tree["clusters/acme-one/addons/argocd-ingress/gateway.yaml"]
    )
    assert "customer_repository_writer_token" not in "".join(tree.values())


def test_render_tree_rejects_non_ipv4_ingress_vip() -> None:
    with pytest.raises(CustomerGitOpsError, match="not an IPv4"):
        render_tree(
            repo_url="https://platform.sunet.se/VDC/customer-acme-clusters-test.git",
            slug="acme-one",
            hostname="argocd.acme-one.k8s-test.sunetvdc.se",
            ingress_vip="not-an-ip",
            interface="ens3",
            acme_contact="noc@sunet.se",
        )
