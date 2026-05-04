"""FastAPI integration tests for the cluster + kubeconfig + request flows.

Hits the real router stack and DB; mocks the two side-effecting boundaries:
  - app.kubeconfig_service.* (no real OpenBao or tenant cluster K8s API)
The git_backend is replaced with a tiny in-memory stub so creating a cluster
and the backup-enable flow don't try to push to a real git repo.

Uses httpx.AsyncClient with ASGITransport so the app and the test share one
asyncio event loop — fastapi.testclient.TestClient runs the ASGI app on its
own AnyIO portal loop, which produces "Future attached to a different loop"
errors when the test fixtures (DB session) are pytest-asyncio-loop bound.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import auth, kubeconfig_service
from app.main import app as fastapi_app
from app.models import Contract, ContractAccess, Customer, KubeconfigIssuance


# ---------------- Stubs ----------------


class StubGitBackend:
    """In-memory stand-in for the GitBackend used in tests."""

    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}

    def write_project(self, *, contract_number, project_name, description, users, managed=False):
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
        }
        return rn

    def get_project(self, resource_name):
        return self.projects.get(resource_name)

    def list_projects(self, contract_number=None):
        out = list(self.projects.values())
        if contract_number is not None:
            out = [p for p in out if p["contract_number"] == contract_number]
        return out

    def update_project(
        self, *, resource_name, description=None, users=None, role_bindings=None
    ):
        p = self.projects.get(resource_name)
        if not p:
            raise ValueError(f"Project '{resource_name}' not found")
        if description is not None:
            p["description"] = description
        if role_bindings is not None:
            p["role_bindings"] = role_bindings
            extracted: list[str] = []
            for rb in role_bindings:
                extracted.extend(rb.get("users", []))
            p["users"] = extracted
        elif users is not None:
            p["users"] = list(users)
        return p

    def delete_project(self, resource_name):
        if resource_name not in self.projects:
            raise ValueError(f"Project '{resource_name}' not found")
        del self.projects[resource_name]


# ---------------- Fixtures ----------------


@pytest.fixture
def git_backend(monkeypatch):
    backend = StubGitBackend()
    fastapi_app.state.git_backend = backend
    return backend


@pytest.fixture
def mock_kubeconfig_service(monkeypatch):
    """Replace the tenant-cluster-touching parts of the issuance flow."""
    from datetime import datetime, timedelta, timezone

    async def fake_issue(cluster, *, user_sub, label, ttl_days, session):
        from app.models import KubeconfigIssuance
        import uuid

        issuance_id = uuid.uuid4().hex
        iss = KubeconfigIssuance(
            cluster_id=cluster.id,
            user_sub=user_sub,
            label=label,
            cert_serial=issuance_id[:16],
            rolebinding_name=f"portal-{issuance_id}",
            cert_group=f"tenant-cluster-{cluster.slug}-issuance-{issuance_id}",
            expires_at=(datetime.now(timezone.utc) + timedelta(days=ttl_days)).replace(tzinfo=None),
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

        rows = (await session.execute(
            select(KubeconfigIssuance).where(
                KubeconfigIssuance.cluster_id == cluster.id,
                KubeconfigIssuance.user_sub == user_sub,
                KubeconfigIssuance.revoked_at.is_(None),
            )
        )).scalars().all()
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
    from app.routers import kubeconfig as kc_router
    from app.routers import clusters as cl_router
    monkeypatch.setattr(kc_router.kubeconfig_service, "issue", fake_issue)
    monkeypatch.setattr(kc_router.kubeconfig_service, "revoke", fake_revoke)
    monkeypatch.setattr(cl_router.kubeconfig_service, "cascade_revoke_for_user", fake_cascade)

    return {"revoke_calls": revoke_calls, "cascade_calls": cascade_calls}


def _login_as(sub: str):
    """Override get_current_user to return a fixed identity."""
    fastapi_app.dependency_overrides[auth.get_current_user] = lambda: {
        "sub": sub, "name": sub, "email": sub,
    }


def _require_admin_for(admin_subs: set[str]):
    """Override require_admin to be permissive for the configured admin subs."""
    from fastapi import HTTPException, Request

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
    # Patch the function used by routers/auth.is_sunet_admin checks via Settings.
    # The simplest path: override get_settings() to return a Settings whose
    # admin_users includes our test admins. Tests below set this when needed.
    from app.config import Settings as RealSettings, get_settings as real_get_settings
    real = real_get_settings()
    overridden = RealSettings(
        oidc_issuer=real.oidc_issuer,
        oidc_client_id=real.oidc_client_id,
        oidc_client_secret=real.oidc_client_secret,
        oidc_redirect_uri=real.oidc_redirect_uri,
        secret_key=real.secret_key,
        database_url=real.database_url,
        git_repo_url=real.git_repo_url,
        admin_users=list(admin_subs),
    )
    fastapi_app.dependency_overrides[real_get_settings] = lambda: overridden


@pytest.fixture
async def client(session, git_backend, mock_kubeconfig_service):
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
    async with AsyncClient(
        transport=transport, base_url=base, headers={"Origin": base}
    ) as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()


# ---------------- Helpers ----------------


async def seed_customer_contract(session, *, name="Acme", domain="acme", contract_number="CO-001"):
    customer = Customer(name=name, domain=domain)
    session.add(customer)
    await session.flush()
    contract = Contract(customer_id=customer.id, contract_number=contract_number)
    session.add(contract)
    await session.flush()
    return customer, contract


async def grant_contract_access(session, contract_id: int, user_sub: str):
    session.add(ContractAccess(contract_id=contract_id, user_sub=user_sub))
    await session.flush()


# ---------------- Tests ----------------


async def test_admin_creates_cluster_and_provisions(client, session):
    _, contract = await seed_customer_contract(session)
    await session.commit()
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})

    payload = {
        "contract_number": "CO-001",
        "name": "Acme prod",
        "slug": "acme-prod",
        "api_url": "https://k8s.acme.test:6443",
        "ca_bundle": "-----BEGIN CERTIFICATE-----\nXYZ\n-----END CERTIFICATE-----\n",
        "worker_groups": 2,
    }
    r = await client.post("/api/admin/clusters", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["slug"] == "acme-prod"
    assert data["size_label"] == "Mellan"
    assert data["total_servers"] == 9
    assert data["provisioned_at"] is None
    assert data["management_project_resource_name"] == "acme-prod-acme"

    # Mark provisioned.
    r = await client.post("/api/admin/clusters/acme-prod/provision")
    assert r.status_code == 200
    assert r.json()["provisioned_at"] is not None


async def test_customer_admin_grants_user_access(client, session):
    customer, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })
    # SUNET admin grants customer_admin
    r = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"})
    assert r.status_code == 201, r.text

    # Now customer admin grants a regular user.
    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/users", json={"user_sub": "bob@org", "role": "user"})
    assert r.status_code == 201, r.text

    # Customer admin can NOT grant another customer_admin.
    r = await client.post("/api/clusters/c1/users", json={"user_sub": "eve@org", "role": "customer_admin"})
    assert r.status_code == 403


async def test_cross_cluster_isolation(client, session):
    await seed_customer_contract(session, name="A", domain="a-org", contract_number="CO-A")
    await seed_customer_contract(session, name="B", domain="b-org", contract_number="CO-B")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    for slug, cn in [("ca", "CO-A"), ("cb", "CO-B")]:
        r = await client.post("/api/admin/clusters", json={
            "contract_number": cn, "name": slug, "slug": slug,
            "api_url": "https://x", "ca_bundle": "X",
        })
        assert r.status_code == 201, r.text

    # alice is customer_admin on A only.
    _ = await client.post("/api/clusters/ca/users", json={"user_sub": "alice@org", "role": "customer_admin"})

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
        _ = await client.post("/api/admin/clusters", json={
            "contract_number": cn, "name": slug, "slug": slug,
            "api_url": "https://x", "ca_bundle": "X",
        })
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

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })
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

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })
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
    rows = (await session.execute(
        __import__("sqlalchemy").select(KubeconfigIssuance).where(
            KubeconfigIssuance.user_sub == "alice@org"
        )
    )).scalars().all()
    assert all(r.revoked_at is not None for r in rows)
    assert len(rows) == 2


async def test_addon_request_apply_and_disable_ui_state(client, session):
    _, contract = await seed_customer_contract(session)
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"})

    # Customer admin requests JupyterHub.
    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/requests", json={
        "request_type": "addon",
        "payload": {"action": "enable", "addon_type": "jupyterhub"},
    })
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

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X", "worker_groups": 1,
    })
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"})

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/requests", json={
        "request_type": "resize", "payload": {"target_worker_groups": 3},
    })
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

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X", "worker_groups": 3,
    })
    _ = await client.post("/api/admin/clusters/c1/provision")
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"})

    _login_as("alice@org")
    _is_sunet_admin_for(set())
    r = await client.post("/api/clusters/c1/requests", json={
        "request_type": "resize", "payload": {"target_worker_groups": 2},
    })
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

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })

    rn = "c1-acme"
    # Initially, no customer_admins → roleBindings on the managed project
    # should be empty/default.
    assert git_backend.projects[rn]["users"] == []

    # Grant first customer_admin.
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "alice@org", "role": "customer_admin"})
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org"], "userDomain": "sso-users"},
    ]

    # Grant a second customer_admin → both should appear, sorted.
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "bob@org", "role": "customer_admin"})
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org", "bob@org"], "userDomain": "sso-users"},
    ]

    # Grant a *regular* user — managed project should NOT change (regular
    # users only get K8s argocd access, not OpenStack reader).
    _ = await client.post("/api/clusters/c1/users", json={"user_sub": "charlie@org", "role": "user"})
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["alice@org", "bob@org"], "userDomain": "sso-users"},
    ]

    # Revoke a customer_admin → that user disappears from the project.
    r = await client.delete("/api/clusters/c1/users/alice@org")
    assert r.status_code == 204
    assert git_backend.projects[rn]["role_bindings"] == [
        {"role": "reader", "users": ["bob@org"], "userDomain": "sso-users"},
    ]


async def test_managed_project_blocks_customer_admin_mutation(client, session, git_backend):
    _, contract = await seed_customer_contract(session)
    await grant_contract_access(session, contract.id, "alice@org")
    _login_as("admin@test")
    _require_admin_for({"admin@test"})
    _is_sunet_admin_for({"admin@test"})
    await session.commit()

    _ = await client.post("/api/admin/clusters", json={
        "contract_number": "CO-001", "name": "c", "slug": "c1",
        "api_url": "https://x", "ca_bundle": "X",
    })
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
    r = await client.delete(f"/api/contracts/CO-001/projects/{rn}")
    assert r.status_code == 403

    # SUNET admin: PATCH allowed
    _login_as("admin@test")
    _is_sunet_admin_for({"admin@test"})
    r = await client.patch(f"/api/contracts/CO-001/projects/{rn}", json={"description": "by admin"})
    assert r.status_code == 200
