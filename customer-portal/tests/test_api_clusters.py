"""FastAPI integration tests for the cluster + kubeconfig + request flows.

Hits the real router stack and DB; mocks the side-effecting boundaries:
  - app.kubeconfig_service.* (no real OpenBao or tenant cluster K8s API)
The project and cluster-manifest git backends are replaced with tiny
in-memory stubs so tests do not push to real repositories.

Uses httpx.AsyncClient with ASGITransport so the app and the test share one
asyncio event loop — fastapi.testclient.TestClient runs the ASGI app on its
own AnyIO portal loop, which produces "Future attached to a different loop"
errors when the test fixtures (DB session) are pytest-asyncio-loop bound.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app import auth, kubeconfig_service, openbao_client, repository_service
from app.cluster_git_backend import ClusterDeletionBlocked
from app.cluster_quotas import managed_cluster_quotas
from app.config import get_settings
from app.git_backend import managed_role_bindings
from app.main import app as fastapi_app
from app.models import (
    ClusterAccess,
    ClusterAddon,
    ClusterGitOps,
    ClusterRequest,
    Contract,
    ContractAccess,
    Customer,
    CustomerClusterRepository,
    GitOpsOperation,
    KubeconfigIssuance,
    TenantCluster,
)

# ---------------- Stubs ----------------


class StubGitBackend:
    """In-memory stand-in for the GitBackend used in tests."""

    def __init__(self, publication_order: list[str]) -> None:
        self.projects: dict[str, dict] = {}
        self.publication_order = publication_order

    def write_project(
        self,
        *,
        contract_number,
        project_name,
        description,
        users,
        managed=False,
        quotas=None,
    ):
        from app.git_backend import _sanitize_name

        rn = _sanitize_name(project_name)
        if rn in self.projects:
            raise ValueError(f"Project '{rn}' already exists")
        self.projects[rn] = {
            "resource_name": rn,
            "name": project_name,
            "description": description,
            "contract_number": contract_number,
            "users": list(users),
            "managed": managed,
            "quotas": quotas,
            "role_bindings": (
                managed_role_bindings(get_settings(), list(users))
                if managed
                else [
                    {
                        "role": "member",
                        "users": list(users),
                        "userDomain": get_settings().default_domain,
                    }
                ]
            ),
        }
        self.publication_order.append("project")
        return rn

    def get_project(self, resource_name):
        return self.projects.get(resource_name)

    def list_projects(self, contract_number=None):
        out = list(self.projects.values())
        if contract_number is not None:
            out = [p for p in out if p["contract_number"] == contract_number]
        return out

    def update_project(self, *, resource_name, description=None, users=None, role_bindings=None):
        p = self.projects.get(resource_name)
        if not p:
            raise ValueError(f"Project '{resource_name}' not found")
        if description is not None:
            p["description"] = description
        if role_bindings is not None:
            p["role_bindings"] = role_bindings
            extracted: list[str] = []
            for rb in role_bindings:
                if p.get("managed") and rb.get("role") != "reader":
                    continue
                extracted.extend(rb.get("users", []))
            p["users"] = extracted
        elif users is not None:
            p["users"] = list(users)
            if p.get("managed"):
                p["role_bindings"] = managed_role_bindings(get_settings(), list(users))
        return p

    def delete_project(self, resource_name):
        if resource_name not in self.projects:
            raise ValueError(f"Project '{resource_name}' not found")
        del self.projects[resource_name]


class StubClusterGitBackend:
    """In-memory stand-in for the cluster desired-state repository."""

    def __init__(self, publication_order: list[str]) -> None:
        self.clusters: dict[str, dict] = {}
        self.publication_order = publication_order

    def exists(self, slug):
        return slug in self.clusters

    def write_cluster(self, **values):
        if values.get("argocd_alias") is None:
            values.pop("argocd_alias", None)
        slug = values["slug"]
        if self.exists(slug):
            if self.clusters[slug] == values:
                return f"clusters/{slug}/cluster.yaml"
            raise ValueError(f"Cluster manifest '{slug}' already exists")
        self.clusters[slug] = values
        self.publication_order.append("cluster")
        return f"clusters/{slug}/cluster.yaml"

    def update_argocd_alias(self, slug, argocd_alias):
        if slug not in self.clusters:
            raise ValueError(f"Cluster manifest '{slug}' not found")
        if argocd_alias is None:
            self.clusters[slug].pop("argocd_alias", None)
        else:
            self.clusters[slug]["argocd_alias"] = argocd_alias

    def delete_cluster(self, slug):
        if slug not in self.clusters:
            raise ValueError(f"Cluster manifest '{slug}' not found")
        raise ClusterDeletionBlocked(
            f"Cluster '{slug}' has a managed manifest; portal deletion is disabled"
        )


# ---------------- Fixtures ----------------


@pytest.fixture(autouse=True)
def cluster_settings(monkeypatch):
    monkeypatch.setenv("CLUSTER_ENVIRONMENT", "test")
    monkeypatch.setenv("MANAGED_CLUSTER_NAMESPACE", "openstack-operator")


@pytest.fixture
def no_repository_secrets(monkeypatch):
    secret_client = Mock(side_effect=AssertionError("Create must not access OpenBao"))
    monkeypatch.setattr(openbao_client, "get_openbao", secret_client)
    monkeypatch.setattr(repository_service, "get_openbao", secret_client)
    return secret_client


@pytest.fixture
def publication_order():
    return []


@pytest.fixture
def git_backend(monkeypatch, publication_order):
    backend = StubGitBackend(publication_order)
    fastapi_app.state.git_backend = backend
    return backend


@pytest.fixture
def cluster_git_backend(publication_order):
    backend = StubClusterGitBackend(publication_order)
    fastapi_app.state.cluster_git_backend = backend
    return backend


@pytest.fixture
def mock_kubeconfig_service(monkeypatch):
    """Replace the tenant-cluster-touching parts of the issuance flow."""
    from datetime import datetime, timedelta, timezone

    async def fake_issue(cluster, *, user_sub, label, ttl_days, session):
        import uuid

        from app.models import KubeconfigIssuance

        issuance_id = uuid.uuid4().hex
        iss = KubeconfigIssuance(
            cluster_id=cluster.id,
            user_sub=user_sub,
            label=label,
            cert_serial=issuance_id[:16],
            rolebinding_name=f"portal-{issuance_id}",
            cert_group=f"tenant-cluster-{cluster.slug}-issuance-{issuance_id}",
            expires_at=(datetime.now(timezone.utc) + timedelta(days=ttl_days)).replace(
                tzinfo=None
            ),
        )
        session.add(iss)
        await session.flush()
        return iss, "apiVersion: v1\nkind: Config\n# stub kubeconfig\n"

    revoke_calls: list[Any] = []

    async def fake_revoke(cluster, issuance, *, by_sub, session):
        from datetime import datetime, timezone

        if issuance.revoked_at is not None:
            return
        issuance.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        issuance.revoked_by_sub = by_sub
        revoke_calls.append((cluster.slug, issuance.id, by_sub))
        await session.flush()

    cascade_calls: list[Any] = []

    async def fake_cascade(cluster, *, user_sub, by_sub, session):
        from datetime import datetime, timezone

        from sqlalchemy import select

        rows = (
            (
                await session.execute(
                    select(KubeconfigIssuance).where(
                        KubeconfigIssuance.cluster_id == cluster.id,
                        KubeconfigIssuance.user_sub == user_sub,
                        KubeconfigIssuance.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for r in rows:
            r.revoked_at = now
            r.revoked_by_sub = by_sub
        await session.flush()
        cascade_calls.append((cluster.slug, user_sub, len(rows)))
        return len(rows)

    monkeypatch.setattr(kubeconfig_service, "issue", fake_issue)
    monkeypatch.setattr(kubeconfig_service, "revoke", fake_revoke)
    monkeypatch.setattr(kubeconfig_service, "cascade_revoke_for_user", fake_cascade)
    # Also monkeypatch where the routers imported them.
    from app.routers import clusters as cl_router
    from app.routers import kubeconfig as kc_router

    monkeypatch.setattr(kc_router.kubeconfig_service, "issue", fake_issue)
    monkeypatch.setattr(kc_router.kubeconfig_service, "revoke", fake_revoke)
    monkeypatch.setattr(cl_router.kubeconfig_service, "cascade_revoke_for_user", fake_cascade)

    return {"revoke_calls": revoke_calls, "cascade_calls": cascade_calls}


def _login_as(sub: str):
    """Override get_current_user to return a fixed identity."""
    fastapi_app.dependency_overrides[auth.get_current_user] = lambda: {
        "sub": sub,
        "name": sub,
        "email": sub,
    }


def _require_admin_for(admin_subs: set[str]):
    """Override require_admin to be permissive for the configured admin subs."""
    from fastapi import HTTPException

    def _impl(request: Request = None):
        # The override receives no Request injection in dependency_overrides; we
        # rely on get_current_user already being overridden, then re-derive sub.
        # We can't access request.session here, so look it up from the override.
        user_factory = fastapi_app.dependency_overrides.get(auth.get_current_user)
        user = user_factory() if user_factory else {"sub": ""}
        if user["sub"] not in admin_subs:
            raise HTTPException(status_code=403, detail="Admin access required")
        return user

    fastapi_app.dependency_overrides[auth.require_admin] = _impl


def _is_sunet_admin_for(admin_subs: set[str]):
    overridden = replace(get_settings(), admin_users=list(admin_subs))
    fastapi_app.dependency_overrides[get_settings] = lambda: overridden


@pytest.fixture
async def client(
    session,
    git_backend,
    cluster_git_backend,
    mock_kubeconfig_service,
):
    """An httpx.AsyncClient against the FastAPI app sharing one event loop."""
    fastapi_app.dependency_overrides.clear()

    async def _get_session():
        yield session

    from app.db import get_session as real_get_session

    fastapi_app.dependency_overrides[real_get_session] = _get_session

    # BASE_URL matches conftest; the CSRF middleware enforces Origin against
    # it, so all test requests send a same-origin Origin header by default.
    import os

    base = os.environ["BASE_URL"]
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url=base, headers={"Origin": base}) as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()


# ---------------- Helpers ----------------


async def seed_customer_contract(
    session, *, name="Acme", domain="acme", contract_number="CO-001", repository=True,
):
    customer = Customer(name=name, domain=domain)
    session.add(customer)
    await session.flush()
    contract = Contract(customer_id=customer.id, contract_number=contract_number)
    session.add(contract)
    if repository:
        session.add(CustomerClusterRepository(
            customer_id=customer.id,
            environment="test",
            repo_url=f"https://platform.sunet.se/vdc/customer-{domain}-clusters-test.git",
            writer_username="portal-writer",
            version=1,
            writer_secret_version=1,
            validation_status="valid",
        ))
    await session.flush()
    return customer, contract


async def grant_contract_access(session, contract_id: int, user_sub: str):
    session.add(ContractAccess(contract_id=contract_id, user_sub=user_sub))
    await session.flush()


def generated_ca(
    *, ca: bool = True, expired: bool = False, not_yet_valid: bool = False,
    constraints: bool = True, signing_usage: bool | None = None,
) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Cluster test CA")])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=1) if not_yet_valid else now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=365))
    )
    if constraints:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=ca, path_length=None), critical=True,
        )
    if signing_usage is not None:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=signing_usage,
            crl_sign=signing_usage, encipher_only=False, decipher_only=False,
        ), critical=True)
    certificate = builder.sign(key, hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


async def complete_cluster(client, slug: str) -> str:
    current = await client.get(f"/api/admin/clusters/{slug}")
    assert current.status_code == 200, current.text
    ca_bundle = generated_ca()
    response = await client.patch(
        f"/api/admin/clusters/{slug}",
        json={
            "config_version": current.json()["config_version"],
            "api_url": f"https://{current.json()['api_hostname']}:6443",
            "ca_bundle": ca_bundle,
        },
    )
    assert response.status_code == 200, response.text
    return ca_bundle


# ---------------- Tests ----------------


@pytest.fixture
async def planned_cluster(client, session):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme prod", "slug": "acme-prod"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def concurrent_member_client(client, planned_cluster, engine):
    """Use independent DB sessions and request identities for concurrent member operations."""
    await complete_cluster(client, "acme-prod")
    response = await client.post("/api/admin/clusters/acme-prod/provision")
    assert response.status_code == 200, response.text
    for user_sub, role in (("alice@org", "customer_admin"), ("bob@org", "user")):
        response = await client.post(
            "/api/clusters/acme-prod/users", json={"user_sub": user_sub, "role": role},
        )
        assert response.status_code == 201, response.text

    from app.db import get_session

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def isolated_session():
        async with factory() as isolated:
            yield isolated

    def current_user(request: Request) -> dict[str, str]:
        return {"sub": request.headers["X-Test-User"]}

    fastapi_app.dependency_overrides[get_session] = isolated_session
    fastapi_app.dependency_overrides[auth.get_current_user] = current_user
    return client


async def wait_for_database_block(engine: AsyncEngine, blocker_pid: int) -> None:
    """Observe a real lock wait rather than assuming the second request has reached the DB."""
    async with engine.connect() as connection:
        # Activity snapshots are transaction-cached, including which backends exist.
        # Each poll must see connections opened after the first sample.
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        while not await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                 "WHERE :blocker = ANY(pg_blocking_pids(pid)))"),
            {"blocker": blocker_pid},
        ):
            await asyncio.sleep(0.01)


async def test_database_block_observer_detects_backend_opened_after_first_poll(
    session, engine, planned_cluster,
):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.cluster_edit import locked_cluster

    await locked_cluster("acme-prod", session)
    blocker_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    first_poll = asyncio.Event()
    waiter_connected = asyncio.Event()
    waiter_pid = 0
    waiter_engine = create_async_engine(engine.url, poolclass=NullPool)

    def observe_poll(connection, cursor, statement, parameters, context, executemany):
        if "pg_blocking_pids" in statement:
            first_poll.set()

    async def wait_for_cluster_lock():
        nonlocal waiter_pid
        async with waiter_engine.connect() as connection:
            waiter_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            waiter_connected.set()
            await connection.execute(
                select(TenantCluster.id).where(TenantCluster.id == planned_cluster["id"])
                .with_for_update()
            )

    event.listen(engine.sync_engine, "after_cursor_execute", observe_poll)
    try:
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            observation = tasks.create_task(wait_for_database_block(engine, blocker_pid))
            await first_poll.wait()
            waiter = tasks.create_task(wait_for_cluster_lock())
            await waiter_connected.wait()
            # Direct PID lookup is independent of the observer's activity snapshot.
            while not await session.scalar(
                text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                {"blocker": blocker_pid, "waiter": waiter_pid},
            ):
                await asyncio.sleep(0.01)
            await observation
            assert not waiter.done()
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", observe_poll)
        await waiter_engine.dispose()


async def test_admin_plans_completes_and_provisions_cluster(
    client,
    session,
    git_backend,
    cluster_git_backend,
    publication_order,
):
    _, contract = await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})

    payload = {
        "contract_number": "CO-001",
        "name": "Acme prod",
        "slug": "acme-prod",
        "worker_groups": 2,
    }
    r = await client.post("/api/admin/clusters", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["slug"] == "acme-prod"
    assert data["customer_id"] == contract.customer_id
    assert data["environment"] == "test"
    assert data["config_version"] == 1
    assert data["size_label"] == "Mellan"
    assert data["total_servers"] == 9
    assert data["provisioned_at"] is None
    assert data["api_url"] is None
    assert data["connection_configured"] is False
    assert data["management_project_resource_name"] == "acme-prod-acme"
    assert data["manifest_path"] == "clusters/acme-prod/cluster.yaml"
    assert data["api_hostname"] == "api.acme-prod.k8s-test.sunetvdc.se"
    assert data["argocd_hostname"] == "argocd.acme-prod.k8s-test.sunetvdc.se"
    assert data["openbao_secret_root"] == "kv/customer-clusters/acme-prod"
    assert git_backend.projects["acme-prod-acme"]["managed"] is True
    assert git_backend.projects["acme-prod-acme"]["role_bindings"] == [
        {"role": "reader", "users": [], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]
    assert git_backend.projects["acme-prod-acme"]["quotas"]["compute"] == {
        "instances": 10,
        "cores": 31,
        "ramMB": 110 * 1024,
    }
    assert cluster_git_backend.clusters["acme-prod"]["worker_groups"] == 2
    assert publication_order == ["project", "cluster"]

    # An incomplete planned cluster cannot be marked provisioned.
    r = await client.post("/api/admin/clusters/acme-prod/provision")
    assert r.status_code == 409

    await complete_cluster(client, "acme-prod")
    r = await client.post("/api/admin/clusters/acme-prod/provision")
    assert r.status_code == 200
    assert r.json()["provisioned_at"] is not None


async def test_admin_creates_updates_and_clears_argocd_alias(
    client,
    session,
    cluster_git_backend,
):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})

    response = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "Acme prod",
            "slug": "acme-prod",
            "argocd_alias": "argocd.example.org",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["argocd_alias"] == "argocd.example.org"
    assert response.json()["argocd_hostname"] == ("argocd.acme-prod.k8s-test.sunetvdc.se")
    assert cluster_git_backend.clusters["acme-prod"]["argocd_alias"] == ("argocd.example.org")

    _login_as("other@test")
    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"argocd_alias": "other.example.org", "config_version": 1},
    )
    assert response.status_code == 403

    _login_as("admin@test")
    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"argocd_alias": "new.example.org", "config_version": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["argocd_alias"] == "new.example.org"
    assert response.json()["config_version"] == 2
    assert cluster_git_backend.clusters["acme-prod"]["argocd_alias"] == ("new.example.org")

    response = await client.patch("/api/admin/clusters/acme-prod", json={})
    assert response.status_code == 200, response.text
    assert response.json()["argocd_alias"] == "new.example.org"
    assert response.json()["config_version"] == 2

    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"argocd_alias": None, "config_version": 2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["argocd_alias"] is None
    assert response.json()["config_version"] == 3
    assert "argocd_alias" not in cluster_git_backend.clusters["acme-prod"]


async def test_api_rejects_invalid_argocd_alias(client, session):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})

    response = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "Acme prod",
            "slug": "acme-prod",
            "argocd_alias": "https://argocd.example.org",
        },
    )
    assert response.status_code == 422

    response = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "Acme prod",
            "slug": "acme-prod",
        },
    )
    assert response.status_code == 201, response.text

    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"argocd_alias": "ArgoCD.example.org"},
    )
    assert response.status_code == 422
    assert (await client.get("/api/admin/clusters/acme-prod")).json()["argocd_alias"] is None


async def test_admin_cluster_retry_rejects_then_adopts_matching_project(
    client,
    session,
    git_backend,
    cluster_git_backend,
    publication_order,
):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    resource_name = "acme-prod-acme"
    git_backend.projects[resource_name] = {
        "resource_name": resource_name,
        "name": "acme-prod.acme",
        "description": "SUNET-managed Kubernetes cluster acme-prod",
        "contract_number": "CO-001",
        "users": [],
        "managed": True,
        "quotas": managed_cluster_quotas(1),
        "role_bindings": [],
    }
    payload = {
        "contract_number": "CO-001",
        "name": "Acme prod",
        "slug": "acme-prod",
        "worker_groups": 2,
    }

    response = await client.post("/api/admin/clusters", json=payload)

    assert response.status_code == 409
    assert "different values: quotas" in response.json()["detail"]
    assert cluster_git_backend.clusters == {}
    assert publication_order == []

    git_backend.projects[resource_name]["quotas"] = managed_cluster_quotas(2)
    cluster_git_backend.clusters["acme-prod"] = {
        "slug": "acme-prod",
        "display_name": "Acme prod",
        "contract_number": "CO-001",
        "customer_domain": "acme",
        "worker_groups": 2,
        "project_name": "acme-prod.acme",
        "project_resource_name": resource_name,
    }
    response = await client.post("/api/admin/clusters", json=payload)

    assert response.status_code == 201, response.text
    assert publication_order == []
    assert git_backend.projects[resource_name]["role_bindings"] == [
        {"role": "reader", "users": [], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]


async def test_admin_delete_safety_refusal_preserves_all_state(
    client,
    session,
    git_backend,
    cluster_git_backend,
):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "Acme prod",
            "slug": "acme-prod",
        },
    )
    assert response.status_code == 201, response.text
    response = await client.delete("/api/admin/clusters/acme-prod")

    assert response.status_code == 409
    assert "portal deletion is disabled" in response.json()["detail"]
    assert "acme-prod" in cluster_git_backend.clusters
    assert "acme-prod-acme" in git_backend.projects
    response = await client.get("/api/admin/clusters/acme-prod")
    assert response.status_code == 200


async def test_admin_delete_allows_db_only_legacy_record(client, session):
    _, contract = await seed_customer_contract(session)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Legacy unpublished",
        slug="legacy-unpublished",
        openbao_mount="kubernetes/legacy-unpublished",
        created_by_sub="admin@test",
    )
    session.add(cluster)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})

    response = await client.delete("/api/admin/clusters/legacy-unpublished")

    assert response.status_code == 204
    response = await client.get("/api/admin/clusters/legacy-unpublished")
    assert response.status_code == 404


async def test_admin_delete_refuses_legacy_record_with_project_state(
    client,
    session,
    git_backend,
    cluster_git_backend,
):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "Acme prod",
            "slug": "acme-prod",
        },
    )
    assert response.status_code == 201, response.text
    del cluster_git_backend.clusters["acme-prod"]

    response = await client.delete("/api/admin/clusters/acme-prod")

    assert response.status_code == 409
    assert "managed project state" in response.json()["detail"]
    assert "acme-prod-acme" in git_backend.projects
    response = await client.get("/api/admin/clusters/acme-prod")
    assert response.status_code == 200


async def test_customer_admin_grants_user_access(client, session):
    customer, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )
    # SUNET admin grants customer_admin
    r = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )
    assert r.status_code == 201, r.text

    # Now customer admin grants a regular user.
    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/users", json={"user_sub": "bob@org", "role": "user"})
    assert r.status_code == 201, r.text

    # Customer admin can NOT grant another customer_admin.
    r = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "eve@org", "role": "customer_admin"}
    )
    assert r.status_code == 403


async def test_customer_admin_updates_only_own_cluster_argocd_alias(
    client,
    session,
    cluster_git_backend,
):
    await seed_customer_contract(session, name="A", domain="a-org", contract_number="CO-A")
    await seed_customer_contract(session, name="B", domain="b-org", contract_number="CO-B")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    for slug, contract_number in (("ca", "CO-A"), ("cb", "CO-B")):
        response = await client.post(
            "/api/admin/clusters",
            json={
                "contract_number": contract_number,
                "name": slug,
                "slug": slug,
            },
        )
        assert response.status_code == 201, response.text

    response = await client.post(
        "/api/clusters/ca/users",
        json={"user_sub": "alice@org", "role": "customer_admin"},
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/clusters/ca/users",
        json={"user_sub": "bob@org", "role": "user"},
    )
    assert response.status_code == 201, response.text

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    response = await client.patch(
        "/api/clusters/ca/argocd-alias",
        json={"argocd_alias": "argocd.customer.example.org"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["caller_role"] == "customer_admin"
    assert response.json()["argocd_alias"] == "argocd.customer.example.org"
    assert response.json()["config_version"] == 2
    assert response.json()["argocd_hostname"] == "argocd.ca.k8s-test.sunetvdc.se"
    assert cluster_git_backend.clusters["ca"]["argocd_alias"] == ("argocd.customer.example.org")

    _login_as("bob@org")
    response = await client.get("/api/clusters/ca")
    assert response.status_code == 200, response.text
    assert response.json()["argocd_alias"] == "argocd.customer.example.org"
    response = await client.patch(
        "/api/clusters/ca/argocd-alias",
        json={"argocd_alias": "argocd.member.example.org"},
    )
    assert response.status_code == 403
    assert cluster_git_backend.clusters["ca"]["argocd_alias"] == ("argocd.customer.example.org")

    _login_as("alice@org")
    response = await client.patch(
        "/api/clusters/cb/argocd-alias",
        json={"argocd_alias": "argocd.other.example.org"},
    )
    assert response.status_code == 403
    assert "argocd_alias" not in cluster_git_backend.clusters["cb"]

    response = await client.patch(
        "/api/clusters/ca/argocd-alias",
        json={"argocd_alias": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["argocd_alias"] is None
    assert response.json()["config_version"] == 3
    assert "argocd_alias" not in cluster_git_backend.clusters["ca"]

    _login_as("admin@test")
    _is_sunet_admin_for({"admin@test"})
    response = await client.patch(
        "/api/clusters/cb/argocd-alias",
        json={"argocd_alias": "argocd.sunet.example.org"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["caller_role"] == "sunet_admin"
    assert cluster_git_backend.clusters["cb"]["argocd_alias"] == ("argocd.sunet.example.org")


async def test_cross_cluster_isolation(client, session):
    await seed_customer_contract(session, name="A", domain="a-org", contract_number="CO-A")
    await seed_customer_contract(session, name="B", domain="b-org", contract_number="CO-B")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    for slug, cn in [("ca", "CO-A"), ("cb", "CO-B")]:
        r = await client.post(
            "/api/admin/clusters",
            json={
                "contract_number": cn,
                "name": slug,
                "slug": slug,
            },
        )
        assert r.status_code == 201, r.text

    # alice is customer_admin on A only.
    _ = await client.post(
        "/api/clusters/ca/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    # Sees A.
    r = await client.get("/api/clusters/ca")
    assert r.status_code == 200
    # Cannot see B.
    r = await client.get("/api/clusters/cb")
    assert r.status_code == 403
    # Cannot grant on B.
    r = await client.post("/api/clusters/cb/users", json={"user_sub": "x@y", "role": "user"})
    assert r.status_code == 403


async def test_user_can_only_see_their_own_clusters(client, session):
    await seed_customer_contract(session, contract_number="CO-1")
    await seed_customer_contract(session, name="X", domain="x", contract_number="CO-2")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    for slug, cn in [("c1", "CO-1"), ("c2", "CO-2")]:
        _ = await client.post(
            "/api/admin/clusters",
            json={
                "contract_number": cn,
                "name": slug,
                "slug": slug,
            },
        )
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "u@org", "role": "user"})

    _login_as("u@org")
    _is_sunet_admin_for(set())
    r = await client.get("/api/clusters")
    assert r.status_code == 200
    slugs = {c["slug"] for c in r.json()}
    assert slugs == {"c1"}


async def test_issue_kubeconfig_requires_provisioning(client, session, mock_kubeconfig_service):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "user@org", "role": "user"})

    _login_as("user@org")
    _is_sunet_admin_for(set())
    # Pre-provisioning, issuance is rejected.
    r = await client.post("/api/clusters/c1/credentials", json={"label": "laptop"})
    assert r.status_code == 409

    # Provision then issue.
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await complete_cluster(client, "c1")
    _ = await client.post("/api/admin/clusters/c1/provision")

    _login_as("user@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/credentials", json={"label": "laptop"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "kubeconfig" in body
    assert body["status"] == "active"
    assert body["label"] == "laptop"

    # Listing returns the issuance.
    r = await client.get("/api/clusters/c1/credentials")
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_cascade_revoke_on_access_removal(client, session, mock_kubeconfig_service):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )
    await complete_cluster(client, "c1")
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "user"})

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    _ = await client.post("/api/clusters/c1/credentials", json={"label": "laptop"})
    _ = await client.post("/api/clusters/c1/credentials", json={"label": "ci"})

    # Admin removes access.
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    r = await client.delete("/api/clusters/c1/users/alice@org")
    assert r.status_code == 204
    assert mock_kubeconfig_service["cascade_calls"] == [("c1", "alice@org", 2)]

    # Issuances are now all revoked.
    rows = (
        (
            await session.execute(
                __import__("sqlalchemy")
                .select(KubeconfigIssuance)
                .where(KubeconfigIssuance.user_sub == "alice@org")
            )
        )
        .scalars()
        .all()
    )
    assert all(r.revoked_at is not None for r in rows)
    assert len(rows) == 2


@pytest.mark.parametrize("first", ["issue", "revoke"])
async def test_access_revocation_serializes_with_credential_issuance(
    concurrent_member_client, session, engine, monkeypatch, first,
):
    client = concurrent_member_client
    started = asyncio.Event()
    release = asyncio.Event()
    blocker_pid = 0
    service_name = "issue" if first == "issue" else "cascade_revoke_for_user"
    original = getattr(kubeconfig_service, service_name)

    async def pause_first_operation(*args, **kwargs):
        nonlocal blocker_pid
        # Pause issuance after authorization, or revocation after scanning existing credentials.
        result = await original(*args, **kwargs) if first == "revoke" else None
        blocker_pid = await kwargs["session"].scalar(text("SELECT pg_backend_pid()"))
        started.set()
        await release.wait()
        return await original(*args, **kwargs) if first == "issue" else result

    monkeypatch.setattr(kubeconfig_service, service_name, pause_first_operation)

    async def issue():
        return await client.post(
            "/api/clusters/acme-prod/credentials", json={"label": "laptop"},
            headers={"X-Test-User": "alice@org"},
        )

    async def revoke():
        return await client.delete(
            "/api/clusters/acme-prod/users/alice@org", headers={"X-Test-User": "admin@test"},
        )

    first_request, second_request = (issue, revoke) if first == "issue" else (revoke, issue)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        first_task = tasks.create_task(first_request())
        await started.wait()
        second_task = tasks.create_task(second_request())
        await wait_for_database_block(engine, blocker_pid)
        release.set()

    responses = [first_task.result(), second_task.result()]
    assert [response.status_code for response in responses] == (
        [201, 204] if first == "issue" else [204, 403]
    ), [response.text for response in responses]
    assert await session.scalar(
        select(ClusterAccess.id).where(ClusterAccess.user_sub == "alice@org")
    ) is None
    issuances = (await session.scalars(
        select(KubeconfigIssuance).where(KubeconfigIssuance.user_sub == "alice@org")
    )).all()
    assert len(issuances) == (1 if first == "issue" else 0)
    assert all(issuance.revoked_at is not None for issuance in issuances)


@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_access_mutation_authorizes_after_waiting_for_revoked_customer_admin(
    concurrent_member_client, session, engine, monkeypatch, operation,
):
    client = concurrent_member_client
    started = asyncio.Event()
    release = asyncio.Event()
    blocker_pid = 0
    cascade = kubeconfig_service.cascade_revoke_for_user

    async def pause_admin_removal(cluster, *, user_sub, by_sub, session):
        nonlocal blocker_pid
        result = await cascade(cluster, user_sub=user_sub, by_sub=by_sub, session=session)
        if user_sub == "alice@org":
            blocker_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            await release.wait()
        return result

    monkeypatch.setattr(kubeconfig_service, "cascade_revoke_for_user", pause_admin_removal)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        removal = tasks.create_task(client.delete(
            "/api/clusters/acme-prod/users/alice@org", headers={"X-Test-User": "admin@test"},
        ))
        await started.wait()
        if operation == "grant":
            mutation = tasks.create_task(client.post(
                "/api/clusters/acme-prod/users", json={"user_sub": "carol@org", "role": "user"},
                headers={"X-Test-User": "alice@org"},
            ))
        else:
            mutation = tasks.create_task(client.delete(
                "/api/clusters/acme-prod/users/bob@org", headers={"X-Test-User": "alice@org"},
            ))
        await wait_for_database_block(engine, blocker_pid)
        release.set()

    assert removal.result().status_code == 204, removal.result().text
    assert mutation.result().status_code == 403, mutation.result().text
    assert mutation.result().json()["detail"] == "No access to this cluster"
    assert set(await session.scalars(select(ClusterAccess.user_sub))) == {"bob@org"}


async def test_addon_request_apply_and_disable_ui_state(client, session):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )
    await complete_cluster(client, "c1")
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )

    # Customer admin requests JupyterHub.
    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post(
        "/api/clusters/c1/requests",
        json={
            "request_type": "addon",
            "payload": {"action": "enable", "addon_type": "jupyterhub"},
        },
    )
    assert r.status_code == 201, r.text
    req_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # Admin applies.
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    r = await client.post(f"/api/admin/cluster-requests/{req_id}/apply", json={"note": "ok"})
    assert r.status_code == 200
    assert r.json()["status"] == "applied"

    # Now the cluster shows the addon active.
    r = await client.get("/api/admin/clusters/c1")
    assert "jupyterhub" in r.json()["active_addons"]


async def test_resize_apply_records_before_count(client, session):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
            "worker_groups": 1,
        },
    )
    await complete_cluster(client, "c1")
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post(
        "/api/clusters/c1/requests",
        json={
            "request_type": "resize",
            "payload": {"target_worker_groups": 3},
        },
    )
    assert r.status_code == 201, r.text
    req_id = r.json()["id"]

    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    r = await client.post(f"/api/admin/cluster-requests/{req_id}/apply", json={"note": None})
    assert r.status_code == 200
    payload = r.json()["payload"]
    assert payload["before_worker_groups"] == 1
    assert payload["target_worker_groups"] == 3

    r = await client.get("/api/admin/clusters/c1")
    assert r.json()["worker_groups"] == 3


async def test_invalid_resize_target_rejected_at_request_time(client, session):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
            "worker_groups": 3,
        },
    )
    await complete_cluster(client, "c1")
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post(
        "/api/clusters/c1/requests",
        json={
            "request_type": "resize",
            "payload": {"target_worker_groups": 2},
        },
    )
    assert r.status_code == 400
    assert "must be > current" in r.json()["detail"]


async def test_customer_admin_grant_syncs_managed_project_readers(client, session, git_backend):
    """Granting customer_admin must rewrite the management project's
    roleBindings so the operator can assign Keystone reader to that user."""
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )

    rn = "c1-acme"
    # Initially, no customer_admins → roleBindings on the managed project
    # should be empty/default.
    assert git_backend.projects[rn]["users"] == []

    # Grant first customer_admin.
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"}
    )
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org"], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]

    # Grant a second customer_admin → both should appear, sorted.
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "bob@org", "role": "customer_admin"}
    )
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org", "bob@org"], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]

    # Grant a *regular* user — managed project should NOT change (regular
    # users only get K8s argocd access, not OpenStack reader).
    _ = await client.post(
        "/api/clusters/c1/users", json={"user_sub": "charlie@org", "role": "user"}
    )
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org", "bob@org"], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]

    # Revoke a customer_admin → that user disappears from the project.
    r = await client.delete("/api/clusters/c1/users/alice@org")
    assert r.status_code == 204
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["bob@org"], "userDomain": "sso-users"},
        {"role": "member", "users": ["admin@test"], "userDomain": "sso-users"},
        {
            "role": "member",
            "users": ["openstack-operator"],
            "userDomain": "default",
        },
    ]


async def test_managed_project_blocks_all_generic_mutation(client, session, git_backend):
    customer, contract = await seed_customer_contract(session)
    session.add(
        Contract(
            customer_id=customer.id,
            contract_number="CO-002",
            description="Move target",
        )
    )
    await grant_contract_access(session, contract.id, "alice@org")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post(
        "/api/admin/clusters",
        json={
            "contract_number": "CO-001",
            "name": "c",
            "slug": "c1",
        },
    )
    rn = "c1-acme"
    assert git_backend.projects[rn]["managed"] is True

    # Customer admin: GET visible
    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.get(f"/api/contracts/CO-001/projects/{rn}")
    assert r.status_code == 200
    assert r.json()["managed"] is True

    # Customer admin: PATCH/DELETE forbidden
    r = await client.patch(f"/api/contracts/CO-001/projects/{rn}", json={"description": "edit"})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    assert "coordinated" in r.json()["detail"]
    r = await client.delete(f"/api/contracts/CO-001/projects/{rn}")
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    assert "coordinated" in r.json()["detail"]

    # SUNET admins must also use the cluster-specific workflow.
    _login_as("admin@test")
    _is_sunet_admin_for({"admin@test"})
    r = await client.patch(
        f"/api/contracts/CO-001/projects/{rn}", json={"description": "by admin"}
    )
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    r = await client.delete(f"/api/contracts/CO-001/projects/{rn}")
    assert r.status_code == 403
    assert "coordinated" in r.json()["detail"]
    assert rn in git_backend.projects

    # Admin relocation and contract rename cannot mutate managed project state.
    r = await client.post(
        f"/api/admin/projects/{rn}/move",
        json={"contract_number": "CO-002"},
    )
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    assert "coordinated" in r.json()["detail"]
    assert git_backend.projects[rn]["contract_number"] == "CO-001"

    r = await client.post(
        f"/api/admin/contracts/{contract.id}/rename",
        json={"contract_number": "CO-003"},
    )
    assert r.status_code == 409
    assert "identity is locked" in r.json()["detail"]
    assert git_backend.projects[rn]["contract_number"] == "CO-001"


async def test_create_reuses_shared_repository_without_openbao(
    client, session, no_repository_secrets, planned_cluster,
):
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Second cluster", "slug": "acme-two"},
    )
    assert response.status_code == 201, response.text
    repositories = (await session.scalars(select(CustomerClusterRepository))).all()
    assert len(repositories) == 1
    repository = repositories[0]
    associations = (await session.scalars(select(ClusterGitOps))).all()
    assert {row.cluster_id for row in associations} == {
        planned_cluster["id"], response.json()["id"],
    }
    assert {row.repository_id for row in associations} == {repository.id}
    assert {row.environment for row in associations} == {"test"}
    assert {row.version for row in associations} == {1}
    assert all(row.published_at is None for row in associations)
    assert repository.version == 1
    assert repository.writer_secret_version == 1
    assert repository.validation_status == "valid"
    no_repository_secrets.assert_not_called()


@pytest.mark.parametrize(
    ("writer_version", "validation_status"),
    [(None, "valid"), (0, "valid"), (-1, "valid"),
     (1, "unvalidated"), (1, "invalid"), (1, "error")],
)
async def test_create_requires_valid_shared_writer(
    client, session, publication_order, no_repository_secrets, writer_version, validation_status,
):
    await seed_customer_contract(session)
    repository = await session.scalar(select(CustomerClusterRepository))
    repository.writer_secret_version = writer_version
    repository.validation_status = validation_status
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme", "slug": "acme"},
    )
    assert response.status_code == 409, response.text
    assert "validate" in response.json()["detail"]
    assert publication_order == []
    assert await session.scalar(select(TenantCluster.id)) is None
    no_repository_secrets.assert_not_called()


async def test_create_requires_preconfigured_shared_repository(
    client, session, publication_order, no_repository_secrets,
):
    await seed_customer_contract(session, repository=False)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme", "slug": "acme"},
    )
    assert response.status_code == 409, response.text
    assert "shared test repository" in response.json()["detail"]
    assert publication_order == []
    assert await session.scalar(select(CustomerClusterRepository.id)) is None
    no_repository_secrets.assert_not_called()


async def test_create_requires_explicit_environment(
    client, session, monkeypatch, publication_order,
):
    await seed_customer_contract(session)
    await session.commit()
    monkeypatch.delenv("CLUSTER_ENVIRONMENT")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme", "slug": "acme"},
    )
    assert response.status_code == 503, response.text
    assert "explicitly test or prod" in response.json()["detail"]
    assert publication_order == []


@pytest.mark.parametrize("field", [
    "customer_repository_url", "customer_repository_writer_username",
    "customer_repository_writer_token", "customer_repository_reader_username",
    "customer_repository_reader_token", "token",
])
async def test_create_rejects_repository_and_token_fields(client, field, publication_order):
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme", "slug": "acme", field: "rejected"},
    )
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", field] for error in response.json()["detail"])
    assert publication_order == []


async def test_existing_cluster_repair_cannot_use_create(
    client, session, no_repository_secrets, publication_order,
):
    _, contract = await seed_customer_contract(session)
    cluster = TenantCluster(
        contract_id=contract.id, name="Existing legacy", slug="legacy",
        openbao_mount="kubernetes/legacy", created_by_sub="admin@test",
    )
    session.add(cluster)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Replacement", "slug": "legacy"},
    )
    assert response.status_code == 409, response.text
    assert "slug already in use" in response.json()["detail"]
    assert await session.scalar(select(ClusterGitOps.cluster_id)) is None
    await session.refresh(cluster)
    assert cluster.name == "Existing legacy"
    assert cluster.config_version == 1
    assert publication_order == []
    no_repository_secrets.assert_not_called()


async def test_create_commit_failure_retries_matching_manifests_without_secrets(
    client, session, monkeypatch, publication_order, no_repository_secrets,
):
    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    payload = {"contract_number": "CO-001", "name": "Acme", "slug": "acme"}

    async def fail_commit():
        await session.flush()
        assert await session.scalar(select(TenantCluster.id)) is not None
        assert await session.scalar(select(ClusterGitOps.cluster_id)) is not None
        raise RuntimeError("Simulated database commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Simulated database commit failure"):
            await client.post("/api/admin/clusters", json=payload)

    assert await session.scalar(select(TenantCluster.id)) is None
    assert await session.scalar(select(ClusterGitOps.cluster_id)) is None
    response = await client.post("/api/admin/clusters", json=payload)
    assert response.status_code == 201, response.text
    assert publication_order == ["project", "cluster"]
    association = await session.get(ClusterGitOps, response.json()["id"])
    assert association.environment == "test"
    assert association.version == 1
    no_repository_secrets.assert_not_called()


async def test_create_locks_customer_then_contract_then_repository(
    client, session, engine, monkeypatch,
):
    from sqlalchemy import event

    await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    locks = []

    def record_locks(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            locks.append(statement)

    real_get_repository = repository_service.get_repository

    async def get_repository(*args, **kwargs):
        assert "FROM customer " in locks[0]
        assert "FROM contract " in locks[1]
        assert kwargs["lock"] is True
        return await real_get_repository(*args, **kwargs)

    monkeypatch.setattr(repository_service, "get_repository", get_repository)
    event.listen(engine.sync_engine, "before_cursor_execute", record_locks)
    try:
        response = await client.post(
            "/api/admin/clusters",
            json={"contract_number": "CO-001", "name": "Acme", "slug": "acme"},
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_locks)
    assert response.status_code == 201, response.text


async def test_create_rereads_contract_owner_after_customer_lock(
    client, session, engine, monkeypatch, publication_order,
):
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    _, contract = await seed_customer_contract(session)
    other = Customer(name="Other", domain="other")
    session.add(other)
    await session.commit()
    contract_id, other_id = contract.id, other.id
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    execute = session.execute
    moved = False

    async def move_before_lock(statement, *args, **kwargs):
        nonlocal moved
        if not moved and "FROM customer " in str(statement) and "FOR UPDATE" in str(statement):
            moved = True
            async with async_sessionmaker(engine)() as concurrent:
                await concurrent.execute(
                    update(Contract).where(Contract.id == contract_id).values(customer_id=other_id)
                )
                await concurrent.commit()
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", move_before_lock)
    response = await client.post(
        "/api/admin/clusters",
        json={"contract_number": "CO-001", "name": "Acme", "slug": "acme"},
    )
    assert moved
    assert response.status_code == 409, response.text
    assert "ownership or identity changed" in response.json()["detail"]
    assert publication_order == []


@pytest.mark.parametrize("runtime_environment", ["prod", ""])
async def test_response_uses_associated_environment(
    client, planned_cluster, monkeypatch, runtime_environment,
):
    monkeypatch.setenv("CLUSTER_ENVIRONMENT", runtime_environment)
    response = await client.get("/api/admin/clusters/acme-prod")
    assert response.status_code == 200, response.text
    assert response.json()["environment"] == "test"
    assert response.json()["customer_id"] == planned_cluster["customer_id"]


async def test_legacy_response_requires_explicit_runtime_environment(client, session, monkeypatch):
    _, contract = await seed_customer_contract(session)
    session.add(TenantCluster(
        contract_id=contract.id, name="Legacy", slug="legacy-prod",
        openbao_mount="kubernetes/legacy-prod", created_by_sub="admin@test",
    ))
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    response = await client.get("/api/admin/clusters/legacy-prod")
    assert response.status_code == 200, response.text
    assert response.json()["environment"] == "test"
    monkeypatch.delenv("CLUSTER_ENVIRONMENT")
    response = await client.get("/api/admin/clusters/legacy-prod")
    assert response.status_code == 503, response.text


async def test_bootstrap_requires_preview_without_publication(
    client, session, no_repository_secrets, planned_cluster, publication_order,
):
    response = await client.post("/api/admin/clusters/acme-prod/bootstrap-gitops")
    assert response.status_code == 410, response.text
    assert "preview before publishing" in response.json()["detail"]
    assert "/api/admin/clusters/acme-prod/gitops" in response.json()["detail"]
    assert publication_order == ["project", "cluster"]
    assert await session.scalar(select(GitOpsOperation.id)) is None
    cluster = await session.get(TenantCluster, planned_cluster["id"])
    assert cluster.provisioned_at is None
    no_repository_secrets.assert_not_called()


async def test_metadata_patch_requires_current_version_and_preserves_connection(
    client, session, planned_cluster, cluster_git_backend,
):
    ca_bundle = await complete_cluster(client, "acme-prod")
    response = await client.patch("/api/admin/clusters/acme-prod", json={"name": "New label"})
    assert response.status_code == 422, response.text
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"name": "New label", "config_version": 1},
    )
    assert response.status_code == 409, response.text
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"name": "New label", "config_version": 2},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "New label"
    assert data["config_version"] == 3
    assert data["connection_configured"] is True
    assert data["provisioned_at"] is None
    assert data["management_project_resource_name"] == (
        planned_cluster["management_project_resource_name"]
    )
    assert cluster_git_backend.clusters["acme-prod"]["display_name"] == "Acme prod"
    cluster = await session.get(TenantCluster, planned_cluster["id"])
    assert cluster.ca_bundle == ca_bundle
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"name": "New label", "config_version": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_version"] == 3


@pytest.mark.parametrize("field", [
    "openbao_role", "argocd_role_name", "argocd_namespace", "openbao_mount", "worker_groups",
    "initial_worker_groups", "slug", "contract_number", "provisioned_at",
])
async def test_metadata_patch_rejects_unsupported_infrastructure_fields(
    client, planned_cluster, field,
):
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"config_version": 1, field: "unsupported"},
    )
    assert response.status_code == 422, response.text
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])
    response = await client.get("/api/admin/clusters/acme-prod")
    assert response.json() == planned_cluster


@pytest.mark.parametrize("field", ["api_url", "ca_bundle"])
async def test_initial_connection_requires_complete_pair(client, planned_cluster, field):
    values = {
        "api_url": f"https://{planned_cluster['api_hostname']}:6443",
        "ca_bundle": generated_ca(),
    }
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"config_version": 1, field: values[field]},
    )
    assert response.status_code == 422, response.text
    current = (await client.get("/api/admin/clusters/acme-prod")).json()
    assert current["config_version"] == 1
    assert current["connection_configured"] is False


async def test_connection_rejects_other_cluster_hostname(client, planned_cluster):
    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"config_version": 1, "ca_bundle": generated_ca(),
              "api_url": "https://api.other.k8s-test.sunetvdc.se:6443"},
    )
    assert response.status_code == 422, response.text
    assert planned_cluster["api_hostname"] in response.json()["detail"]


@pytest.mark.parametrize("invalid_ca", [
    "garbage", "client", "private-key", "mixed-key", "expired", "not-yet-valid",
    "missing-constraints", "wrong-key-usage",
])
async def test_connection_rejects_invalid_ca_material(client, planned_cluster, invalid_ca):
    key_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    ca_bundle = {
        "garbage": "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
        "client": generated_ca(ca=False),
        "private-key": key_pem,
        "mixed-key": generated_ca() + key_pem,
        "expired": generated_ca(expired=True),
        "not-yet-valid": generated_ca(not_yet_valid=True),
        "missing-constraints": generated_ca(constraints=False),
        "wrong-key-usage": generated_ca(signing_usage=False),
    }[invalid_ca]
    response = await client.patch(
        "/api/admin/clusters/acme-prod",
        json={"config_version": 1, "ca_bundle": ca_bundle,
              "api_url": f"https://{planned_cluster['api_hostname']}:6443"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("change", ["ca", "endpoint"])
async def test_active_issuance_guards_connection_changes_but_allows_idempotent_values(
    client, session, planned_cluster, monkeypatch, cluster_git_backend, change,
):
    ca_bundle = await complete_cluster(client, "acme-prod")
    provision = await client.post("/api/admin/clusters/acme-prod/provision")
    assert provision.status_code == 200, provision.text
    issuance = await client.post("/api/clusters/acme-prod/credentials", json={"label": "laptop"})
    assert issuance.status_code == 201, issuance.text
    values = {"config_version": 2, "ca_bundle": ca_bundle,
              "api_url": f"https://{planned_cluster['api_hostname']}:6443"}
    response = await client.patch("/api/admin/clusters/acme-prod", json=values)
    assert response.status_code == 200, response.text
    assert response.json()["config_version"] == 2
    if change == "ca":
        values["ca_bundle"] = generated_ca()
    else:
        monkeypatch.setenv("CLUSTER_DNS_ZONE", "migrated.example.org")
        _is_sunet_admin_for({"admin@test"})
        values["api_url"] = "https://api.acme-prod.migrated.example.org:6443"
    values["name"] = "Must also remain unchanged"
    values["argocd_alias"] = "must-not-publish.example.org"
    response = await client.patch("/api/admin/clusters/acme-prod", json=values)
    assert response.status_code == 409, response.text
    assert "manual migration" in response.json()["detail"]
    cluster = await session.get(TenantCluster, planned_cluster["id"])
    assert cluster.ca_bundle == ca_bundle
    assert cluster.name == planned_cluster["name"]
    assert cluster.argocd_alias is None
    assert "argocd_alias" not in cluster_git_backend.clusters["acme-prod"]
    assert cluster.config_version == 2
    assert cluster.provisioned_at is not None
    credential = await session.get(KubeconfigIssuance, issuance.json()["id"])
    assert credential.revoked_at is None


@pytest.mark.parametrize("history", ["none", "expired", "revoked"])
async def test_live_connection_can_change_without_active_credentials_and_preserves_history(
    client, session, planned_cluster, history,
):
    await complete_cluster(client, "acme-prod")
    provision = await client.post("/api/admin/clusters/acme-prod/provision")
    assert provision.status_code == 200, provision.text
    cluster_id = planned_cluster["id"]
    session.add(ClusterAccess(
        cluster_id=cluster_id, user_sub="member@test", role="user", granted_by_sub="admin@test",
    ))
    now = datetime.now(UTC).replace(tzinfo=None)
    if history != "none":
        session.add(KubeconfigIssuance(
            cluster_id=cluster_id, user_sub="member@test", label="Old laptop", cert_serial="abc",
            rolebinding_name="old-binding", cert_group="old-group",
            expires_at=now + timedelta(days=-1 if history == "expired" else 30),
            revoked_at=now if history == "revoked" else None,
        ))
    session.add(ClusterAddon(
        cluster_id=cluster_id, addon_type="jupyterhub", enabled_by_sub="admin@test",
    ))
    request_record = ClusterRequest(
        cluster_id=cluster_id, request_type="resize", payload='{"before_worker_groups": 1}',
        status="applied", requested_by_sub="member@test", applied_by_sub="admin@test",
        applied_at=now,
    )
    session.add(request_record)
    await session.commit()
    replacement_ca = generated_ca()
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"config_version": 2, "ca_bundle": replacement_ca},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_version"] == 3
    assert response.json()["provisioned_at"] == provision.json()["provisioned_at"]
    assert response.json()["active_addons"] == ["jupyterhub"]
    assert response.json()["worker_groups"] == planned_cluster["worker_groups"]
    assert response.json()["initial_worker_groups"] == planned_cluster["initial_worker_groups"]
    assert await session.scalar(select(ClusterAccess.id)) is not None
    assert await session.get(ClusterRequest, request_record.id) is request_record
    assert request_record.status == "applied"
    cluster = await session.get(TenantCluster, cluster_id)
    assert cluster.ca_bundle == replacement_ca


@pytest.mark.parametrize("endpoint", ["metadata", "customer-alias"])
async def test_concurrent_cluster_edits_are_serialized(
    client, session, engine, planned_cluster, endpoint,
):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import get_session

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def isolated_session():
        async with factory() as isolated:
            yield isolated

    fastapi_app.dependency_overrides[get_session] = isolated_session
    if endpoint == "metadata":
        path = "/api/admin/clusters/acme-prod"
        values = [{"config_version": 1, "name": "A"}, {"config_version": 1, "name": "B"}]
    else:
        path = "/api/clusters/acme-prod/argocd-alias"
        values = [{"argocd_alias": "a.example.org"}, {"argocd_alias": "b.example.org"}]
    responses = await asyncio.wait_for(asyncio.gather(
        *(client.patch(path, json=value) for value in values),
    ), timeout=5)
    expected_statuses = [200, 409] if endpoint == "metadata" else [200, 200]
    assert sorted(response.status_code for response in responses) == expected_statuses
    response = await client.get("/api/admin/clusters/acme-prod")
    if endpoint == "metadata":
        assert response.json()["config_version"] == 2
        assert response.json()["name"] in ("A", "B")
    else:
        assert response.json()["config_version"] == 3
        assert response.json()["argocd_alias"] in ("a.example.org", "b.example.org")


async def test_customer_alias_edit_invalidates_admin_version(client, planned_cluster):
    response = await client.patch(
        "/api/clusters/acme-prod/argocd-alias", json={"argocd_alias": "argocd.customer.example"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_version"] == 2
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"config_version": 1, "argocd_alias": None},
    )
    assert response.status_code == 409, response.text
    response = await client.get("/api/admin/clusters/acme-prod")
    assert response.json()["argocd_alias"] == "argocd.customer.example"


async def test_ca_bundle_preserves_valid_legacy_pem_formatting_for_idempotent_edits(
    client, session, planned_cluster,
):
    cluster = await session.get(TenantCluster, planned_cluster["id"])
    bundle = "\r\n" + (generated_ca(signing_usage=True) + generated_ca()).replace("\n", "\r\n")
    cluster.api_url = f"https://{planned_cluster['api_hostname']}:6443"
    cluster.ca_bundle = bundle
    cluster.provisioned_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    issuance = await client.post("/api/clusters/acme-prod/credentials", json={"label": "laptop"})
    assert issuance.status_code == 201, issuance.text
    response = await client.patch(
        "/api/admin/clusters/acme-prod", json={"config_version": 1, "ca_bundle": bundle},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_version"] == 1
    assert cluster.ca_bundle == bundle


@pytest.mark.parametrize("history", [
    "association", "operation", "provisioned", "expired-issuance", "revoked-issuance",
    "disabled-addon", "denied-request", "api", "ca",
])
async def test_legacy_delete_preserves_all_lifecycle_history(
    client, session, monkeypatch, cluster_git_backend, history,
):
    _, contract = await seed_customer_contract(session)
    cluster = TenantCluster(
        contract_id=contract.id, name="Legacy", slug="legacy",
        openbao_mount="kubernetes/legacy", created_by_sub="admin@test",
    )
    session.add(cluster)
    await session.flush()
    repository = await session.scalar(select(CustomerClusterRepository))
    now = datetime.now(UTC).replace(tzinfo=None)
    record = None
    if history == "association":
        record = ClusterGitOps(
            cluster_id=cluster.id, repository_id=repository.id, environment="test",
        )
    elif history == "operation":
        record = GitOpsOperation(
            id="failed-preview", cluster_id=cluster.id, repository_id=repository.id,
            kind="preview", status="failed", requested_by_sub="admin@test",
        )
    elif history == "provisioned":
        cluster.provisioned_at = now
    elif history.endswith("issuance"):
        record = KubeconfigIssuance(
            cluster_id=cluster.id, user_sub="member@test", label="Old", cert_serial="abc",
            rolebinding_name="binding", cert_group="group", expires_at=now - timedelta(days=1),
            revoked_at=now if history == "revoked-issuance" else None,
        )
    elif history == "disabled-addon":
        record = ClusterAddon(
            cluster_id=cluster.id, addon_type="jupyterhub", enabled_by_sub="admin@test",
            disabled_at=now,
        )
    elif history == "denied-request":
        record = ClusterRequest(
            cluster_id=cluster.id, request_type="resize", payload="{}", status="denied",
            requested_by_sub="member@test",
        )
    elif history == "api":
        cluster.api_url = "https://legacy.example:6443"
    elif history == "ca":
        cluster.ca_bundle = generated_ca()
    if record is not None:
        session.add(record)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    delete = Mock(side_effect=AssertionError("History must be checked before touching Git"))
    monkeypatch.setattr(cluster_git_backend, "delete_cluster", delete)
    response = await client.delete("/api/admin/clusters/legacy")
    assert response.status_code == 409, response.text
    assert "portal deletion is disabled" in response.json()["detail"]
    assert await session.scalar(select(TenantCluster.id)) == cluster.id
    if record is not None:
        assert await session.scalar(select(type(record).cluster_id)) == cluster.id
    delete.assert_not_called()


async def test_legacy_delete_blocks_unknown_cleanup_state(
    client, session, monkeypatch, cluster_git_backend,
):
    _, contract = await seed_customer_contract(session)
    session.add(TenantCluster(
        contract_id=contract.id, name="Legacy", slug="legacy",
        openbao_mount="kubernetes/legacy", created_by_sub="admin@test",
    ))
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    monkeypatch.setattr(
        cluster_git_backend, "delete_cluster",
        Mock(side_effect=ValueError("Unparseable Git state")),
    )
    response = await client.delete("/api/admin/clusters/legacy")
    assert response.status_code == 409, response.text
    assert "cleanup state is unknown" in response.json()["detail"]
    assert await session.scalar(select(TenantCluster.id)) is not None
