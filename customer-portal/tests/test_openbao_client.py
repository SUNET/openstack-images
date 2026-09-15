"""Tests for the OpenBao HTTP client.

Validates the request-shape contract against OpenBao/Vault's API:
  - kubernetes-auth login is POST with {role, jwt} body
  - kubernetes secrets-engine creds are minted via POST (NOT GET — that
    returns 405 unsupported-operation, which we hit in the wild on the
    sunet-test cluster).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import Response

from app.config import Settings, get_settings
from app.openbao_client import OpenBaoCASConflict, OpenBaoClient, OpenBaoError


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
                        "service_account_name": "portal-rbac-manager",
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
            # Status code surfaces; raw response bodies are never included.
            assert "404" in str(exc.value)
            assert "mount not found" not in str(exc.value)
    finally:
        await client.aclose()


KV_PATH = "kv/data/customer-cluster-repositories/42/test/writer"
KV_URL = f"http://openbao.test:8200/v1/{KV_PATH}"
LOGIN_URL = "http://openbao.test:8200/v1/auth/kubernetes/login"
SENSITIVE = "do-not-disclose-secret-token"


@pytest.fixture
async def kv_client(settings: Settings):
    client = OpenBaoClient(settings)
    assert client._http._trust_env is False
    try:
        yield client
    finally:
        await client.aclose()


def login_response(token: str = "session-token", lease: int = 60) -> Response:
    return Response(200, json={"auth": {"client_token": token, "lease_duration": lease}})


async def test_kv_cas_write_and_pinned_read(kv_client):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        write = router.post(KV_URL).mock(return_value=Response(200, json={"data": {"version": 4}}))
        read = router.get(KV_URL, params={"version": 3}).mock(return_value=Response(200, json={
            "data": {"data": {"token": SENSITIVE}, "metadata": {"version": 3}},
        }))
        assert await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=3) == 4
        assert json.loads(write.calls.last.request.content) == {
            "data": {"token": SENSITIVE}, "options": {"cas": 3},
        }
        secret = await kv_client.read_kv_secret_versioned(KV_PATH, version=3)
        assert secret.version == 3
        assert secret.data == {"token": SENSITIVE}
        assert SENSITIVE not in repr(secret)
        assert read.calls.last.request.url.params["version"] == "3"


async def test_kv_create_is_conditional(kv_client):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        write = router.post(KV_URL).mock(return_value=Response(200, json={"data": {"version": 1}}))
        assert await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=0) == 1
        assert json.loads(write.calls.last.request.content)["options"] == {"cas": 0}


async def test_kv_cas_conflict_is_sanitized_without_retry(kv_client, caplog):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        write = router.post(KV_URL).mock(return_value=Response(400, json={"errors": [
            f"check-and-set parameter did not match the current version: {SENSITIVE}",
        ]}))
        with pytest.raises(OpenBaoCASConflict) as exc:
            await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=2)
        assert write.call_count == 1
        assert SENSITIVE not in str(exc.value) + caplog.text


@pytest.mark.parametrize("method", ["read", "write"])
@pytest.mark.parametrize("status", [401, 403])
async def test_kv_relogin_is_bounded(kv_client, method, status):
    with respx.mock() as router:
        login = router.post(LOGIN_URL).mock(
            side_effect=[login_response("one"), login_response("two")]
        )
        route = router.route(method="GET" if method == "read" else "POST", url=KV_URL).mock(
            return_value=Response(status, json={"errors": [SENSITIVE]})
        )
        with pytest.raises(OpenBaoError) as exc:
            if method == "read":
                await kv_client.read_kv_secret(KV_PATH, version=2)
            else:
                await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=2)
        assert login.call_count == route.call_count == 2
        assert SENSITIVE not in str(exc.value)
        assert route.calls[1].request.headers["X-Vault-Token"] == "two"
        if method == "write":
            assert route.calls[0].request.content == route.calls[1].request.content


async def test_short_login_lease_expires_before_actual_ttl(kv_client, monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(
        "app.openbao_client.time", SimpleNamespace(monotonic=lambda: clock.value)
    )
    with respx.mock() as router:
        login = router.post(LOGIN_URL).mock(side_effect=[
            login_response("one", lease=2), login_response("two", lease=2),
        ])
        assert await kv_client._token_or_login() == "one"
        assert 100 < kv_client._token_expires_at < 102
        clock.value = 101
        assert await kv_client._token_or_login() == "one"
        clock.value = 102
        assert await kv_client._token_or_login() == "two"
        assert login.call_count == 2


async def test_concurrent_auth_failures_share_one_relogin(kv_client):
    both_rejected = asyncio.Event()
    rejected = 0

    async def respond(request: httpx.Request) -> Response:
        nonlocal rejected
        if request.headers["X-Vault-Token"] == "one":
            rejected += 1
            if rejected == 2:
                both_rejected.set()
            await asyncio.wait_for(both_rejected.wait(), timeout=2)
            return Response(403)
        return Response(200, json={
            "data": {"data": {"token": SENSITIVE}, "metadata": {"version": 1}},
        })

    with respx.mock() as router:
        login = router.post(LOGIN_URL).mock(
            side_effect=[login_response("one"), login_response("two")]
        )
        router.get(KV_URL).mock(side_effect=respond)
        results = await asyncio.gather(
            kv_client.read_kv_secret(KV_PATH), kv_client.read_kv_secret(KV_PATH)
        )
        assert results == [{"token": SENSITIVE}] * 2
        assert login.call_count == 2


@pytest.mark.parametrize("body", [
    None, [], {"auth": []}, {"auth": {"client_token": SENSITIVE}},
    {"auth": {"client_token": SENSITIVE, "lease_duration": "sixty"}},
    {"auth": {"client_token": SENSITIVE, "lease_duration": True}},
    {"auth": {"client_token": [SENSITIVE], "lease_duration": 60}},
])
async def test_malformed_login_is_sanitized(kv_client, body, caplog):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=Response(200, content=json.dumps(body)))
        with pytest.raises(OpenBaoError) as exc:
            await kv_client._login()
        assert SENSITIVE not in str(exc.value) + caplog.text
        assert kv_client._token is None


@pytest.mark.parametrize("method", ["read", "write", "mint", "login"])
async def test_invalid_json_never_leaks_response(kv_client, method, caplog):
    with respx.mock(assert_all_called=False) as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        response = Response(200, text=f"not-json {SENSITIVE}")
        if method == "login":
            router.post(LOGIN_URL).mock(return_value=response)
        elif method == "mint":
            router.post("http://openbao.test:8200/v1/kubernetes/x/creds/r").mock(
                return_value=response
            )
        else:
            router.route(method="GET" if method == "read" else "POST", url=KV_URL).mock(
                return_value=response
            )
        with pytest.raises(OpenBaoError) as exc:
            if method == "read":
                await kv_client.read_kv_secret(KV_PATH)
            elif method == "write":
                await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=0)
            elif method == "mint":
                await kv_client.get_k8s_creds("kubernetes/x", "r")
            else:
                await kv_client._login()
        assert SENSITIVE not in str(exc.value) + caplog.text
        assert exc.value.__suppress_context__


@pytest.mark.parametrize("body", [
    None, [], {"data": []}, {"data": {"data": {"token": SENSITIVE}}},
    {"data": {"data": {"token": 123}, "metadata": {"version": 1}}},
    {"data": {"data": {"token": SENSITIVE}, "metadata": {"version": True}}},
    {"data": {"data": {"token": SENSITIVE}, "metadata": {"version": 2}}},
])
async def test_malformed_or_wrong_kv_version_is_rejected(kv_client, body):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        router.get(KV_URL).mock(return_value=Response(200, content=json.dumps(body)))
        with pytest.raises(OpenBaoError) as exc:
            await kv_client.read_kv_secret(KV_PATH, version=1)
        assert SENSITIVE not in str(exc.value)


@pytest.mark.parametrize("body", [None, [], {"data": {}}, {"data": {"version": 0}},
                                      {"data": {"version": 3}}])
async def test_write_requires_a_confirmed_next_version(kv_client, body):
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        router.post(KV_URL).mock(return_value=Response(200, content=json.dumps(body)))
        with pytest.raises(OpenBaoError):
            await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=0)


async def test_ambiguous_write_timeout_is_not_replayed(kv_client):
    with respx.mock() as router:
        login = router.post(LOGIN_URL).mock(return_value=login_response())
        write = router.post(KV_URL).mock(side_effect=httpx.ReadTimeout(SENSITIVE))
        with pytest.raises(OpenBaoError) as exc:
            await kv_client.write_kv_secret(KV_PATH, {"token": SENSITIVE}, cas=2)
        assert login.call_count == write.call_count == 1
        assert SENSITIVE not in str(exc.value)
        assert exc.value.__suppress_context__


@pytest.mark.parametrize("path", [
    "kv/data/other/42/test/writer", f"/{KV_PATH}", f"{KV_PATH}?version=2",
    "kv/data/customer-cluster-repositories/42/../writer",
    "kv/data/customer-cluster-repositories/42/test/%77riter",
    "kv/data/customer-cluster-repositories/42/staging/writer",
    "kv/data/customer-cluster-repositories/42/dev/reader",
    "https://evil.test/v1/secret",
])
async def test_arbitrary_kv_paths_rejected_before_authentication(kv_client, path):
    with respx.mock() as router:
        with pytest.raises(OpenBaoError):
            await kv_client.read_kv_secret(path)
        with pytest.raises(OpenBaoError):
            await kv_client.write_kv_secret(path, {"token": SENSITIVE}, cas=0)
        assert not router.calls


async def test_prod_kv_path_is_supported(kv_client):
    path = KV_PATH.replace("/test/", "/prod/")
    with respx.mock() as router:
        router.post(LOGIN_URL).mock(return_value=login_response())
        router.post(f"http://openbao.test:8200/v1/{path}").mock(
            return_value=Response(200, json={"data": {"version": 1}})
        )
        assert await kv_client.write_kv_secret(path, {"token": SENSITIVE}, cas=0) == 1
