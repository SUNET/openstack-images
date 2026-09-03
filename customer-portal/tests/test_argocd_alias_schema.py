"""Focused validation tests for Argo CD alias request fields."""

import pytest
from pydantic import ValidationError

from app.schemas import (
    CreateClusterRequest,
    UpdateArgocdAliasRequest,
    UpdateClusterRequest,
)


@pytest.mark.parametrize(
    "alias",
    [
        "ArgoCD.example.org",
        "argocd.example.org.",
        "https://argocd.example.org",
        "argocd.example.org:443",
        "argocd.example.org/path",
        "*.example.org",
        "argocd.foo_bar.org",
        "192.0.2.1",
        "192.168.001.001",
        "localhost",
        "-argocd.example.org",
        "argocd-.example.org",
        f"{'a' * 64}.example.org",
        "argocd.exämple.org",
        "",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}",
    ],
)
@pytest.mark.parametrize(
    "schema",
    [CreateClusterRequest, UpdateClusterRequest, UpdateArgocdAliasRequest],
)
def test_argocd_alias_rejects_non_fqdn_values(alias, schema) -> None:
    values = {"argocd_alias": alias}
    if schema is CreateClusterRequest:
        values.update(contract_number="CO-001", name="Cluster", slug="cluster")

    with pytest.raises(ValidationError):
        schema(**values)


@pytest.mark.parametrize(
    "schema",
    [CreateClusterRequest, UpdateClusterRequest, UpdateArgocdAliasRequest],
)
def test_argocd_alias_accepts_lowercase_ascii_fqdn_and_null(schema) -> None:
    values = {"argocd_alias": "argocd.customer.example.org"}
    if schema is CreateClusterRequest:
        values.update(contract_number="CO-001", name="Cluster", slug="cluster")

    assert schema(**values).argocd_alias == "argocd.customer.example.org"
    values["argocd_alias"] = None
    assert schema(**values).argocd_alias is None


def test_member_alias_request_requires_only_explicit_alias() -> None:
    with pytest.raises(ValidationError):
        UpdateArgocdAliasRequest()
    with pytest.raises(ValidationError):
        UpdateArgocdAliasRequest(argocd_alias=None, name="not allowed")
