"""Tests for the OpenBao HTTP client.

Validates the request-shape contract against OpenBao/Vault's API:
  - kubernetes-auth login is POST with {role, jwt} body
  - kubernetes secrets-engine creds are minted via POST (NOT GET — that
    returns 405 unsupported-operation, which we hit in the wild on the
    sunet-test cluster).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from app.config import Settings, get_settings
from app.openbao_client import OpenBaoClient, OpenBaoError


@pytest.fixture
def fake_sa_token(tmp_path) -> str:
    p = tmp_path / "sa-token"
    p.write_text("fake.sa.jwt")
    return str(p)


@pytest.fixture
def settings(fake_sa_token, monkeypatch) -> Settings:
    monkeypatch.setenv("OPENBAO_ADDR", "http://openbao.test:8200")
    monkeypatch.setenv("OPENBAO_ALLOW_INSECURE", "1")  # http addr is dev-only
    monkeypatch.setenv("OPENBAO_K8S_AUTH_ROLE", "customer-portal")
    monkeypatch.setenv("OPENBAO_SA_TOKEN_PATH", fake_sa_token)
    # bypass cached settings
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    return Settings()


async def test_login_uses_post_with_jwt(settings: Settings):
    client = OpenBaoClient(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            login_route = router.post("http://openbao.test:8200/v1/auth/kubernetes/login").mock(
                return_value=Response(200, json={
                    "auth": {"client_token": "vault.token.abc", "lease_duration": 3600},
                })
            )
            token = await client._login()
            assert token == "vault.token.abc"
            req = login_route.calls.last.request
            assert req.method == "POST"
            body = req.read().decode()
            assert "customer-portal" in body
            assert "fake.sa.jwt" in body
    finally:
        await client.aclose()


async def test_get_k8s_creds_uses_post(settings: Settings):
    """Regression: this used to be a GET, which hits 405 on real OpenBao."""
    client = OpenBaoClient(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.post("http://openbao.test:8200/v1/auth/kubernetes/login").mock(
                return_value=Response(200, json={
                    "auth": {"client_token": "vault.token.abc", "lease_duration": 3600},
                })
            )
            creds_route = router.post(
                "http://openbao.test:8200/v1/kubernetes/sunet-test/creds/argocd-rbac-manager"
            ).mock(
                return_value=Response(200, json={
                    "data": {
                        "service_account_token": "eyJfake",
                        "service_account_name": "openbao-rbac-manager",
                        "service_account_namespace": "kube-system",
                    },
                })
            )
            data = await client.get_k8s_creds("kubernetes/sunet-test", "argocd-rbac-manager")
            assert data["service_account_token"] == "eyJfake"
            req = creds_route.calls.last.request
            assert req.method == "POST"
            assert req.headers["X-Vault-Token"] == "vault.token.abc"
            # Body must include kubernetes_namespace so multi-namespace roles work.
            import json as _json
            sent = _json.loads(req.read().decode())
            assert sent.get("kubernetes_namespace") == "kube-system"
    finally:
        await client.aclose()


async def test_get_k8s_creds_re_logs_in_on_403(settings: Settings):
    client = OpenBaoClient(settings)
    try:
        with respx.mock() as router:
            login_route = router.post(
                "http://openbao.test:8200/v1/auth/kubernetes/login"
            ).mock(side_effect=[
                Response(200, json={"auth": {"client_token": "tok1", "lease_duration": 60}}),
                Response(200, json={"auth": {"client_token": "tok2", "lease_duration": 60}}),
            ])
            creds_route = router.post(
                "http://openbao.test:8200/v1/kubernetes/x/creds/r"
            ).mock(side_effect=[
                Response(403, json={"errors": ["denied"]}),
                Response(200, json={"data": {"service_account_token": "eyOK"}}),
            ])
            data = await client.get_k8s_creds("kubernetes/x", "r")
            assert data["service_account_token"] == "eyOK"
            assert login_route.call_count == 2
            assert creds_route.call_count == 2
    finally:
        await client.aclose()


async def test_get_k8s_creds_propagates_other_errors(settings: Settings):
    client = OpenBaoClient(settings)
    try:
        with respx.mock() as router:
            router.post("http://openbao.test:8200/v1/auth/kubernetes/login").mock(
                return_value=Response(200, json={
                    "auth": {"client_token": "tok", "lease_duration": 60},
                })
            )
            router.post("http://openbao.test:8200/v1/kubernetes/x/creds/r").mock(
                return_value=Response(404, json={"errors": ["mount not found"]})
            )
            with pytest.raises(OpenBaoError) as exc:
                await client.get_k8s_creds("kubernetes/x", "r")
            # Status code surfaces; raw response body must NOT (it goes to
            # the server log instead, to avoid leaking internal hints).
            assert "404" in str(exc.value)
            assert "mount not found" not in str(exc.value)
    finally:
        await client.aclose()
