"""Repository lifecycle and ownership guards against real PostgreSQL transactions."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest
import respx
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import repository_service
from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    ClusterAccess,
    ClusterGitOps,
    Contract,
    Customer,
    CustomerClusterRepository,
    GitOpsOperation,
    TenantCluster,
)
from app.openbao_client import OpenBaoCASConflict, OpenBaoError, VersionedKVSecret
from app.repository_schemas import RepositoryUpdateRequest
from app.routers import admin, customer_repositories

REPO_URL = "https://platform.sunet.se/vdc/shared.git"
SECRET = "sentinel-token-never-in-db-or-api"


class MemoryBao:
    """Append-only versions and real CAS semantics, with no reader read capability."""

    def __init__(self):
        self.secrets: dict[str, list[dict[str, str]]] = {}
        self.reads: list[tuple[str, int | None]] = []
        self.writes: list[tuple[str, int]] = []
        self.fail_write = False

    async def read_kv_secret_versioned(
        self, path: str, *, version: int | None = None
    ) -> VersionedKVSecret:
        assert path.endswith("/writer"), "The portal has no reader GET capability"
        self.reads.append((path, version))
        versions = self.secrets.get(path, [])
        if not versions or (version is not None and version > len(versions)):
            raise OpenBaoError("Not found", status_code=404)
        selected = version or len(versions)
        return VersionedKVSecret(data=versions[selected - 1].copy(), version=selected)

    async def write_kv_secret(self, path: str, data: dict[str, str], *, cas: int) -> int:
        if self.fail_write:
            raise OpenBaoError(SECRET)
        versions = self.secrets.setdefault(path, [])
        if cas != len(versions):
            raise OpenBaoCASConflict("Version conflict")
        versions.append(data.copy())
        self.writes.append((path, cas))
        return len(versions)


@pytest.fixture
def settings():
    return Settings(
        cluster_environment="test", customer_cluster_bases_revision="a" * 40,
        admin_users=["admin@test"],
    )


@pytest.fixture
def bao(monkeypatch):
    client = MemoryBao()
    monkeypatch.setattr(repository_service, "get_openbao", lambda: client)
    return client


@pytest.fixture
def app(session, engine, settings, bao):
    application = FastAPI()
    application.include_router(customer_repositories.router)
    application.include_router(admin.router)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def request_session():
        async with factory() as current:
            yield current

    @application.middleware("http")
    async def test_session(request: Request, call_next):
        sub = request.headers.get("X-Test-User", "admin@test")
        request.scope["session"] = {} if sub == "anonymous" else {"user": {"sub": sub}}
        return await call_next(request)

    application.dependency_overrides[get_session] = request_session
    application.dependency_overrides[get_settings] = lambda: settings
    application.state.git_backend = Mock()
    application.state.git_backend.list_projects.return_value = []
    application.state.git_backend.rename_contract.return_value = 0
    return application


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://portal.test"
    ) as client:
        yield client


@pytest.fixture
async def customers(session):
    first = Customer(name="Alpha", domain="alpha.test")
    second = Customer(name="Beta", domain="beta.test")
    session.add_all([first, second])
    await session.commit()
    return first, second


def endpoint(customer_id: int) -> str:
    return f"/api/admin/customers/{customer_id}/cluster-repository"


async def configure(client, customer_id: int, repo_url: str = REPO_URL) -> dict:
    response = await client.put(endpoint(customer_id), json={
        "repo_url": repo_url, "expected_version": 0,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def credential(client, customer_id: int, kind: str, version: int, **extra):
    return await client.post(f"{endpoint(customer_id)}/credentials/{kind}", json={
        "expected_version": version, "username": kind,
        "token": SECRET if kind == "writer" else f"reader-{SECRET}", **extra,
    })


async def cluster_for(session, customer_id: int, slug: str = "alpha-cluster") -> TenantCluster:
    contract = Contract(customer_id=customer_id, contract_number=f"C-{slug}")
    session.add(contract)
    await session.flush()
    cluster = TenantCluster(
        contract_id=contract.id, name=slug, slug=slug, created_by_sub="admin@test",
        openbao_mount=f"kubernetes/{slug}",
    )
    session.add(cluster)
    await session.flush()
    return cluster


def forgejo_routes(router, **changes):
    repository = router.get("https://platform.sunet.se/api/v1/repos/vdc/shared").mock(
        return_value=httpx.Response(200, json={
            "private": True, "full_name": "VDC/shared", "clone_url": REPO_URL,
            "permissions": {"push": True, "pull": True}, **changes,
        })
    )
    return repository


@pytest.fixture(autouse=True)
def git_read_access(monkeypatch):
    check = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", check)
    return check


async def test_missing_config_and_nonsecret_create(client, customers, bao, settings):
    customer, _ = customers
    missing = await client.get(endpoint(customer.id))
    assert missing.status_code == 200
    assert missing.json()["configured"] is False
    assert missing.json()["id"] is None
    assert missing.json()["version"] == 0
    assert missing.json()["bases_revision"] == settings.customer_cluster_bases_revision
    saved = await configure(client, customer.id, "https://PLATFORM.sunet.se:443/VDC/Shared")
    assert saved["repo_url"] == REPO_URL
    assert saved["version"] == 1
    assert saved["writer_username"] == ""
    assert not saved["writer_configured"] and not saved["reader_configured"]
    assert not bao.reads and not bao.writes
    assert (await client.get(endpoint(99999))).status_code == 404


@pytest.mark.parametrize("actor,status", [("tenant@test", 403), ("anonymous", 401)])
async def test_all_repository_routes_require_sunet_admin(client, customers, bao, actor, status):
    customer, _ = customers
    path = endpoint(customer.id)
    requests = [
        ("GET", path, None),
        ("PUT", path, {"repo_url": REPO_URL, "expected_version": 0}),
        ("POST", f"{path}/credentials/writer", {
            "expected_version": 1, "username": "writer", "token": SECRET,
        }),
        ("POST", f"{path}/validate", {"expected_version": 1}),
    ]
    for method, url, payload in requests:
        response = await client.request(method, url, json=payload, headers={"X-Test-User": actor})
        assert response.status_code == status
        assert SECRET not in response.text
    assert not bao.reads and not bao.writes


async def test_cluster_customer_admin_cannot_manage_shared_credentials(client, session, customers):
    cluster = await cluster_for(session, customers[0].id)
    session.add(ClusterAccess(
        cluster_id=cluster.id, user_sub="tenant@test", role="customer_admin",
        granted_by_sub="admin@test",
    ))
    await session.commit()
    response = await client.get(endpoint(customers[0].id), headers={"X-Test-User": "tenant@test"})
    assert response.status_code == 403


async def test_environment_isolation_and_repository_reuse(client, session, customers):
    customer, other = customers
    prod = CustomerClusterRepository(
        customer_id=customer.id, environment="prod",
        repo_url="https://platform.sunet.se/vdc/shared-prod.git", writer_username="old",
    )
    session.add(prod)
    await session.commit()
    assert (await client.get(endpoint(customer.id))).json()["configured"] is False
    saved = await configure(client, customer.id)
    first = await cluster_for(session, customer.id, "first")
    second = await cluster_for(session, customer.id, "second")
    await cluster_for(session, other.id, "unrelated")
    session.add_all([
        ClusterGitOps(cluster_id=first.id, repository_id=saved["id"], environment="test"),
        ClusterGitOps(
            cluster_id=second.id, repository_id=saved["id"], environment="test",
            reader_installed_version=7,
        ),
    ])
    await session.commit()
    response = (await client.get(endpoint(customer.id))).json()
    assert response["clusters"] == [
        {"slug": "first", "name": "first", "reader_installed_version": None},
        {"slug": "second", "name": "second", "reader_installed_version": 7},
    ]
    await session.refresh(prod)
    assert prod.writer_username == "old" and prod.version == 1


async def test_cross_customer_legacy_alias_binding_is_rejected(client, session, customers, bao):
    session.add(CustomerClusterRepository(
        customer_id=customers[0].id, environment="test",
        repo_url="https://PLATFORM.sunet.se:443/VDC/Shared/", writer_username="legacy",
    ))
    await session.commit()
    response = await client.put(endpoint(customers[1].id), json={
        "repo_url": REPO_URL, "expected_version": 0,
    })
    assert response.status_code == 409
    assert not bao.reads and not bao.writes


@pytest.mark.parametrize("legacy_url", [
    "https://platform.sunet.se/VDC/Shared.git",
    "https://PLATFORM.sunet.se:443/VDC/Shared/",
])
@pytest.mark.parametrize("kind,status", [
    ("preview", "preview_ready"), ("publish", "queued"), ("publish", "failed"),
])
async def test_equivalent_url_put_preserves_legacy_binding_for_durable_operations(
    client, session, customers, bao, legacy_url, kind, status
):
    customer = customers[0]
    validated_at = datetime(2026, 1, 1)
    repository = CustomerClusterRepository(
        customer_id=customer.id, environment="test", repo_url=legacy_url, version=7,
        writer_username="writer", reader_username="reader",
        writer_secret_version=3, reader_secret_version=2,
        validation_status="valid", validated_at=validated_at,
    )
    session.add(repository)
    await session.flush()
    cluster = await cluster_for(session, customer.id)
    session.add(ClusterGitOps(
        cluster_id=cluster.id, repository_id=repository.id, environment="test", version=2,
    ))
    reviewed_payload = json.dumps({
        "repository_version": 7, "settings_version": 2,
        "preview": {"repo_url": legacy_url},
    })
    operation = GitOpsOperation(
        id="reviewed-operation", cluster_id=cluster.id, repository_id=repository.id,
        kind=kind, status=status, payload=reviewed_payload, requested_by_sub="admin@test",
    )
    session.add(operation)
    await session.commit()

    response = await client.put(endpoint(customer.id), json={
        "repo_url": REPO_URL, "expected_version": 7,
    })
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 7
    await session.refresh(repository)
    await session.refresh(operation)
    assert repository.repo_url == legacy_url and repository.version == 7
    assert repository.writer_secret_version == 3 and repository.reader_secret_version == 2
    assert repository.validation_status == "valid" and repository.validated_at == validated_at
    assert operation.kind == kind and operation.status == status
    assert operation.payload == reviewed_payload and operation.result_commit is None
    payload = json.loads(operation.payload)
    assert payload["preview"]["repo_url"] == repository.repo_url
    assert payload["repository_version"] == repository.version
    assert not bao.reads and not bao.writes


async def test_rotation_uses_cas_and_put_omission_preserves_credentials(
    client, session, customers, bao, caplog
):
    customer = customers[0]
    await configure(client, customer.id)
    writer = await credential(client, customer.id, "writer", 1)
    assert writer.status_code == 200
    assert writer.json()["version"] == 2 and writer.json()["writer_secret_version"] == 1
    reader = await credential(client, customer.id, "reader", 2)
    assert reader.status_code == 200
    assert reader.json()["version"] == 3 and reader.json()["reader_secret_version"] == 1
    unchanged = await client.put(endpoint(customer.id), json={
        "repo_url": REPO_URL, "expected_version": 3,
    })
    assert unchanged.json()["version"] == 3
    assert unchanged.json()["writer_secret_version"] == 1
    assert unchanged.json()["reader_secret_version"] == 1
    rotated = await credential(client, customer.id, "reader", 3)
    assert rotated.status_code == 200 and rotated.json()["reader_secret_version"] == 2
    assert bao.writes[-1] == (
        repository_service.repository_secret_path(customer.id, "test", "reader"), 1,
    )
    for versions in bao.secrets.values():
        assert all(secret["repo_url"] == REPO_URL for secret in versions)
    raw_db = (await session.execute(text(
        "SELECT row_to_json(r)::text FROM customer_cluster_repository r"
    ))).scalars().all()
    assert SECRET not in str(raw_db) + writer.text + reader.text + rotated.text + caplog.text
    stale = await credential(client, customer.id, "writer", 1)
    assert stale.status_code == 409 and len(bao.writes) == 3


async def test_unpublished_url_change_invalidates_old_credential_references(
    client, customers, bao
):
    customer = customers[0]
    await configure(client, customer.id)
    await credential(client, customer.id, "writer", 1)
    await credential(client, customer.id, "reader", 2)
    reads = len(bao.reads)
    changed = await client.put(endpoint(customer.id), json={
        "repo_url": "https://platform.sunet.se/vdc/changed", "expected_version": 3,
    })
    assert changed.status_code == 200
    body = changed.json()
    assert body["version"] == 4 and body["writer_secret_version"] is None
    assert body["reader_secret_version"] is None
    assert not body["writer_configured"] and not body["reader_configured"]
    assert body["validation_status"] == "unvalidated" and body["validated_at"] is None
    validation = await client.post(
        f"{endpoint(customer.id)}/validate", json={"expected_version": 4}
    )
    assert validation.status_code == 409 and len(bao.reads) == reads
    # Existing reader versions require an explicit conditional replacement after a URL change.
    assert (await credential(client, customer.id, "reader", 4)).status_code == 409
    replacement = await credential(client, customer.id, "reader", 4, expected_secret_version=1)
    assert replacement.status_code == 200
    assert bao.secrets[bao.writes[-1][0]][-1]["repo_url"].endswith("/vdc/changed.git")


@pytest.mark.parametrize(
    "state", ["queued", "running", "preview_ready", "published", "historical"]
)
async def test_publication_and_active_operations_lock_url(client, session, customers, state):
    customer = customers[0]
    saved = await configure(client, customer.id)
    cluster = await cluster_for(session, customer.id)
    gitops = ClusterGitOps(cluster_id=cluster.id, repository_id=saved["id"], environment="test")
    session.add(gitops)
    if state == "published":
        gitops.published_at = datetime(2026, 1, 1)
    else:
        session.add(GitOpsOperation(
            id="operation", cluster_id=cluster.id, repository_id=saved["id"], kind="preview",
            status="succeeded" if state == "historical" else state,
            result_commit="a" * 40 if state == "historical" else None,
            requested_by_sub="admin@test",
        ))
    await session.commit()
    response = await client.put(endpoint(customer.id), json={
        "repo_url": "https://platform.sunet.se/vdc/changed", "expected_version": 1,
    })
    assert response.status_code == 409
    # Credentials may rotate; the version change invalidates the preview snapshot.
    rotated = await credential(client, customer.id, "writer", 1)
    assert rotated.status_code == 200 and rotated.json()["version"] == 2


@pytest.mark.parametrize("status", ["failed", "conflict", "succeeded"])
async def test_publish_intent_without_result_permanently_blocks_rebinding(
    client, session, customers, bao, settings, status
):
    customer = customers[0]
    saved = await configure(client, customer.id)
    await credential(client, customer.id, "writer", 1)
    cluster = await cluster_for(session, customer.id)
    session.add(GitOpsOperation(
        id="ambiguous-publish", cluster_id=cluster.id, repository_id=saved["id"],
        kind="publish", status=status, result_commit=None, requested_by_sub="admin@test",
        finished_at=datetime(2026, 1, 1),
    ))
    await session.commit()
    reads, writes = len(bao.reads), len(bao.writes)
    response = await client.put(endpoint(customer.id), json={
        "repo_url": "https://platform.sunet.se/vdc/changed", "expected_version": 2,
    })
    assert response.status_code == 409 and "publication intent" in response.text
    assert (len(bao.reads), len(bao.writes)) == (reads, writes)
    repository = await session.get(CustomerClusterRepository, saved["id"])
    assert repository.repo_url == REPO_URL and repository.version == 2
    assert repository.writer_secret_version == 1

    rotated = await credential(
        client, customer.id, "writer", 2, token="rotated-for-read-only-recovery"
    )
    assert rotated.status_code == 200 and rotated.json()["version"] == 3
    assert rotated.json()["validation_status"] == "unvalidated"
    await session.refresh(repository)
    credentials = await repository_service.writer_credentials(repository, settings)
    assert credentials == ("writer", "rotated-for-read-only-recovery")
    assert bao.reads[-1][1] == 2
    still_locked = await client.put(endpoint(customer.id), json={
        "repo_url": "https://platform.sunet.se/vdc/changed", "expected_version": 3,
    })
    assert still_locked.status_code == 409


async def test_failed_preview_without_publication_intent_allows_url_change(
    client, session, customers
):
    customer = customers[0]
    saved = await configure(client, customer.id)
    cluster = await cluster_for(session, customer.id)
    session.add(GitOpsOperation(
        id="failed-preview", cluster_id=cluster.id, repository_id=saved["id"],
        kind="preview", status="failed", result_commit=None, requested_by_sub="admin@test",
        finished_at=datetime(2026, 1, 1),
    ))
    await session.commit()
    response = await client.put(endpoint(customer.id), json={
        "repo_url": "https://platform.sunet.se/vdc/changed", "expected_version": 1,
    })
    assert response.status_code == 200
    assert response.json()["repo_url"] == "https://platform.sunet.se/vdc/changed.git"


async def test_legacy_reader_requires_explicit_cas_without_reading_it(
    client, session, customers, bao
):
    customer = customers[0]
    repository = CustomerClusterRepository(
        customer_id=customer.id, environment="test", repo_url=REPO_URL,
        writer_username="", reader_username="legacy-reader",
    )
    session.add(repository)
    await session.commit()
    path = repository_service.repository_secret_path(customer.id, "test", "reader")
    bao.secrets[path] = [{"username": "legacy-reader", "token": "old"}]
    missing = await credential(client, customer.id, "reader", 1)
    assert missing.status_code == 409 and "expected_secret_version" in missing.text
    wrong = await credential(client, customer.id, "reader", 1, expected_secret_version=0)
    assert wrong.status_code == 409
    valid = await credential(client, customer.id, "reader", 1, expected_secret_version=1)
    assert valid.status_code == 200 and valid.json()["reader_secret_version"] == 2
    assert not bao.reads


async def test_validation_adopts_legacy_writer_and_labels_evidence(
    client, session, customers, bao, settings, git_read_access
):
    customer = customers[0]
    repository = CustomerClusterRepository(
        customer_id=customer.id, environment="test", repo_url=REPO_URL, writer_username="writer",
    )
    session.add(repository)
    await session.commit()
    path = repository_service.repository_secret_path(customer.id, "test", "writer")
    bao.secrets[path] = [{"username": "writer", "token": SECRET}] * 3
    with respx.mock() as router:
        repo_route = forgejo_routes(router)
        response = await client.post(
            f"{endpoint(customer.id)}/validate", json={"expected_version": 1}
        )
        assert repo_route.calls.last.request.headers["Authorization"] == f"token {SECRET}"
        assert repo_route.calls.last.request.method == "GET"
        assert len(router.calls) == 1
    assert response.status_code == 200
    body = response.json()
    assert body["validation_status"] == "valid" and body["writer_secret_version"] == 3
    assert body["version"] == 2 and "not proof of token scope" in body["validation_message"]
    assert not bao.writes and SECRET not in response.text
    await session.refresh(repository)
    assert await repository_service.writer_credentials(repository, settings) == ("writer", SECRET)
    assert bao.reads[-1] == (path, 3)
    git_read_access.assert_called_once_with(REPO_URL, "writer", SECRET)


async def test_git_validation_failure_does_not_pin_legacy_writer(
    client, session, customers, bao, git_read_access,
):
    from app.gitops_types import CustomerGitOpsError

    customer = customers[0]
    repository = CustomerClusterRepository(
        customer_id=customer.id, environment="test", repo_url=REPO_URL,
        writer_username="writer",
    )
    session.add(repository)
    await session.commit()
    path = repository_service.repository_secret_path(customer.id, "test", "writer")
    bao.secrets[path] = [{"username": "writer", "token": SECRET}]
    git_read_access.side_effect = CustomerGitOpsError(SECRET)
    with respx.mock() as router:
        forgejo_routes(router)
        response = await client.post(
            f"{endpoint(customer.id)}/validate", json={"expected_version": 1}
        )
    assert response.status_code == 200
    assert response.json()["validation_status"] == "error"
    assert "Git read access failed" in response.json()["validation_message"]
    assert SECRET not in response.text
    await session.refresh(repository)
    assert repository.writer_secret_version is None
    assert repository.version == 1
    assert not bao.writes


@pytest.mark.parametrize("changes", [
    {"private": False}, {"private": "true"}, {"full_name": "vdc/other"},
    {"clone_url": "https://evil.test/vdc/shared.git"}, {"permissions": {"push": False}},
])
async def test_validation_requires_private_matching_repository_and_push_permission(
    client, customers, changes, git_read_access
):
    customer = customers[0]
    await configure(client, customer.id)
    await credential(client, customer.id, "writer", 1)
    with respx.mock() as router:
        forgejo_routes(router, **changes)
        response = await client.post(
            f"{endpoint(customer.id)}/validate", json={"expected_version": 2}
        )
    assert response.status_code == 200
    assert response.json()["validation_status"] == "invalid"
    assert SECRET not in response.text
    git_read_access.assert_not_called()


async def test_validation_does_not_follow_redirects_or_echo_malformed_body(client, customers):
    customer = customers[0]
    await configure(client, customer.id)
    await credential(client, customer.id, "writer", 1)
    with respx.mock() as router:
        route = router.get("https://platform.sunet.se/api/v1/repos/vdc/shared").mock(
            return_value=httpx.Response(302, headers={"Location": f"https://evil.test/{SECRET}"})
        )
        response = await client.post(
            f"{endpoint(customer.id)}/validate", json={"expected_version": 2}
        )
        assert response.json()["validation_status"] == "invalid"
        route.mock(return_value=httpx.Response(200, text=f"invalid {SECRET}"))
        response = await client.post(
            f"{endpoint(customer.id)}/validate", json={"expected_version": 2}
        )
        assert response.json()["validation_status"] == "error"
        assert SECRET not in response.text


async def test_secret_write_failure_leaves_db_configuration_active(
    client, session, customers, bao, caplog
):
    customer = customers[0]
    await configure(client, customer.id)
    bao.fail_write = True
    response = await credential(client, customer.id, "reader", 1)
    assert response.status_code == 503
    repository = await session.scalar(select(CustomerClusterRepository))
    assert repository.version == 1 and repository.reader_secret_version is None
    assert SECRET not in response.text + caplog.text


@pytest.mark.parametrize("kind", ["writer", "reader"])
@pytest.mark.parametrize("failure_site", ["commit", "response_flush"])
async def test_failed_commit_returns_metadata_and_allows_explicit_recovery(
    client, session, customers, bao, settings, monkeypatch, kind, failure_site
):
    customer = customers[0]
    saved = await configure(client, customer.id)
    await credential(client, customer.id, kind, 1, token="previous-token")

    async def fail(*args, **kwargs):
        raise SQLAlchemyError(SECRET)

    with monkeypatch.context() as patch:
        if failure_site == "commit":
            patch.setattr(AsyncSession, "commit", fail)
        else:
            patch.setattr(customer_repositories, "repository_response", fail)
        response = await credential(client, customer.id, kind, 2)
    assert response.status_code == 503 and SECRET not in response.text
    detail = response.json()["detail"]
    assert detail["code"] == "credential_commit_failed"
    assert detail["kind"] == kind and detail["repository_version"] == 2
    assert detail["pinned_secret_version"] == detail["expected_secret_version"] == 1
    assert detail["written_secret_version"] == 2 and detail["latest_secret_version"] is None
    repository = await session.get(CustomerClusterRepository, saved["id"])
    assert repository.version == 2 and getattr(repository, f"{kind}_secret_version") == 1
    if kind == "writer":
        assert await repository_service.writer_credentials(repository, settings) == (
            "writer", "previous-token",
        )
    assert len(bao.secrets[bao.writes[-1][0]]) == 2
    assert (await credential(client, customer.id, kind, 2)).status_code == 409
    assert len(bao.writes) == 2
    reloaded = (await client.get(endpoint(customer.id))).json()
    assert reloaded["version"] == 2 and reloaded[f"{kind}_secret_version"] == 1
    recovered = await credential(
        client, customer.id, kind, reloaded["version"],
        expected_secret_version=detail["written_secret_version"],
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["version"] == 3 and recovered.json()[f"{kind}_secret_version"] == 3
    if kind == "reader":
        assert not bao.reads
    else:
        await session.refresh(repository)
        credentials = await repository_service.writer_credentials(repository, settings)
        assert credentials == ("writer", SECRET)
        assert bao.reads[-1][1] == 3


@pytest.mark.parametrize("same_customer", [True, False])
async def test_concurrent_create_has_one_winner_for_owner_or_canonical_url(
    client, session, customers, same_customer
):
    first, second = customers
    results = await asyncio.gather(
        client.put(endpoint(first.id), json={"repo_url": REPO_URL, "expected_version": 0}),
        client.put(endpoint(first.id if same_customer else second.id), json={
            "repo_url": "https://PLATFORM.sunet.se:443/VDC/SHARED/", "expected_version": 0,
        }),
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    assert await session.scalar(select(func.count()).select_from(CustomerClusterRepository)) == 1


async def test_concurrent_rotation_stale_request_never_writes_secret(client, customers, bao):
    await configure(client, customers[0].id)
    results = await asyncio.gather(
        credential(client, customers[0].id, "writer", 1),
        credential(client, customers[0].id, "writer", 1),
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    assert len(bao.writes) == 1


async def test_worker_repository_lock_is_shared_and_version_reloaded(
    client, customers, engine, bao
):
    saved = await configure(client, customers[0].id)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as worker:
        # Use the exact worker protocol independently of the service helper.
        await worker.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('customer-repository'), :id)"
        ), {"id": saved["id"]})
        repository = await worker.get(CustomerClusterRepository, saved["id"])
        repository.version = 2
        task = asyncio.create_task(credential(client, customers[0].id, "reader", 1))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            assert not bao.writes
            await worker.commit()
            response = await asyncio.wait_for(task, timeout=5)
            assert response.status_code == 409 and not bao.writes
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("association", ["cluster", "repository"])
@pytest.mark.parametrize("action", ["move", "rename", "delete", "domain", "customer_delete"])
async def test_admin_identity_changes_are_guarded(
    client, app, session, customers, association, action
):
    customer, target = customers
    if association == "cluster":
        cluster = await cluster_for(session, customer.id)
        contract_id = cluster.contract_id
    else:
        session.add(CustomerClusterRepository(
            customer_id=customer.id, environment="prod", repo_url=REPO_URL, writer_username="",
        ))
        contract = Contract(customer_id=customer.id, contract_number="guarded")
        if action != "customer_delete":
            session.add(contract)
            await session.flush()
        contract_id = contract.id
    await session.commit()
    if action == "move":
        response = await client.post(f"/api/admin/contracts/{contract_id}/move", json={
            "customer_id": target.id,
        })
    elif action == "rename":
        response = await client.post(f"/api/admin/contracts/{contract_id}/rename", json={
            "contract_number": "new-number",
        })
    elif action == "delete":
        response = await client.delete(f"/api/admin/contracts/{contract_id}")
    elif action == "domain":
        response = await client.patch(f"/api/admin/customers/{customer.id}", json={
            "domain": "changed.test",
        })
    else:
        response = await client.delete(f"/api/admin/customers/{customer.id}")
    assert response.status_code == 409, response.text
    app.state.git_backend.list_projects.assert_not_called()


async def test_move_into_customer_with_repository_is_guarded(client, session, customers):
    customer, target = customers
    contract = Contract(customer_id=customer.id, contract_number="movable")
    session.add(contract)
    await session.commit()
    await configure(client, target.id)
    response = await client.post(f"/api/admin/contracts/{contract.id}/move", json={
        "customer_id": target.id,
    })
    assert response.status_code == 409


async def test_unassociated_contract_and_customer_management_remains_available(
    client, session, customers
):
    customer, target = customers
    contract = Contract(customer_id=customer.id, contract_number="movable")
    session.add(contract)
    await session.commit()
    moved = await client.post(f"/api/admin/contracts/{contract.id}/move", json={
        "customer_id": target.id,
    })
    assert moved.status_code == 200
    renamed = await client.post(f"/api/admin/contracts/{contract.id}/rename", json={
        "contract_number": "renamed",
    })
    assert renamed.status_code == 200
    assert (await client.delete(f"/api/admin/contracts/{contract.id}")).status_code == 204
    changed = await client.patch(
        f"/api/admin/customers/{customer.id}", json={"domain": "new.test"}
    )
    assert changed.status_code == 200
    assert (await client.delete(f"/api/admin/customers/{customer.id}")).status_code == 204


async def test_unrecognized_environment_is_disabled(client, app, customers, settings, bao):
    app.dependency_overrides[get_settings] = lambda: replace(
        settings, cluster_environment="staging"
    )
    response = await client.put(endpoint(customers[0].id), json={
        "repo_url": REPO_URL, "expected_version": 0,
    })
    assert response.status_code == 503
    assert not bao.reads and not bao.writes


async def test_same_customer_canonical_repo_cannot_bind_another_environment(
    client, session, customers, bao
):
    customer = customers[0]
    session.add(CustomerClusterRepository(
        customer_id=customer.id, environment="prod", writer_username="",
        repo_url="https://PLATFORM.sunet.se:443/VDC/SHARED/",
    ))
    await session.commit()
    response = await client.put(endpoint(customer.id), json={
        "repo_url": REPO_URL, "expected_version": 0,
    })
    assert response.status_code == 409 and "environment" in response.text
    assert not bao.reads and not bao.writes


async def test_legacy_cross_environment_duplicates_cannot_rotate_or_validate(
    client, session, customers, bao
):
    for environment in ("test", "prod"):
        session.add(CustomerClusterRepository(
            customer_id=customers[0].id, environment=environment, repo_url=REPO_URL,
            writer_username="writer", writer_secret_version=1,
        ))
    await session.commit()
    rotated = await credential(client, customers[0].id, "writer", 1)
    validated = await client.post(
        f"{endpoint(customers[0].id)}/validate", json={"expected_version": 1}
    )
    assert rotated.status_code == validated.status_code == 409
    assert not bao.reads and not bao.writes


async def test_concurrent_environment_bindings_have_one_winner(
    session, engine, customers, settings
):
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def save(environment: str) -> int:
        async with factory() as current:
            try:
                await repository_service.save_repository(
                    customers[0].id,
                    RepositoryUpdateRequest(repo_url=REPO_URL, expected_version=0),
                    replace(settings, cluster_environment=environment),
                    current,
                )
                await current.commit()
                return 200
            except HTTPException as exc:
                await current.rollback()
                return exc.status_code

    assert sorted(await asyncio.gather(save("test"), save("prod"))) == [200, 409]
    assert await session.scalar(select(func.count()).select_from(CustomerClusterRepository)) == 1


async def test_unpinned_writer_replacement_requires_confirmation(client, session, customers, bao):
    saved = await configure(client, customers[0].id)
    path = repository_service.repository_secret_path(customers[0].id, "test", "writer")
    bao.secrets[path] = [{"username": "writer", "token": "legacy-secret"}] * 3
    response = await credential(client, customers[0].id, "writer", 1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "secret_version_confirmation_required"
    assert detail["pinned_secret_version"] is None and detail["latest_secret_version"] == 3
    assert "legacy-secret" not in response.text and SECRET not in response.text
    assert not bao.writes
    repository = await session.get(CustomerClusterRepository, saved["id"])
    assert repository.version == 1 and repository.writer_secret_version is None
    replacement = await credential(
        client, customers[0].id, "writer", 1, expected_secret_version=3
    )
    assert replacement.status_code == 200 and replacement.json()["writer_secret_version"] == 4


@pytest.mark.parametrize("kind", ["writer", "reader"])
@pytest.mark.parametrize("latest", [2, 4])
async def test_cas_recovery_is_explicit_and_conditionally_matches_latest(
    client, session, customers, bao, kind, latest
):
    saved = await configure(client, customers[0].id)
    await credential(client, customers[0].id, kind, 1)
    path = repository_service.repository_secret_path(customers[0].id, "test", kind)
    bao.secrets[path].extend([
        {"username": kind, "token": "orphan-secret", "repo_url": REPO_URL}
        for _ in range(latest - 1)
    ])
    response = await credential(client, customers[0].id, kind, 2)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "secret_version_conflict"
    assert detail["pinned_secret_version"] == detail["expected_secret_version"] == 1
    assert detail["latest_secret_version"] == (latest if kind == "writer" else None)
    assert SECRET not in response.text and "orphan-secret" not in response.text
    assert len(bao.writes) == 1
    incorrect = await credential(
        client, customers[0].id, kind, 2, expected_secret_version=latest + 1
    )
    assert incorrect.status_code == 409
    assert incorrect.json()["detail"]["code"] == "secret_version_conflict"
    assert len(bao.writes) == 1
    repository = await session.get(CustomerClusterRepository, saved["id"])
    assert repository.version == 2 and getattr(repository, f"{kind}_secret_version") == 1
    recovered = await credential(
        client, customers[0].id, kind, 2, expected_secret_version=latest
    )
    assert recovered.status_code == 200
    assert recovered.json()[f"{kind}_secret_version"] == latest + 1
    assert bao.writes[-1] == (path, latest)
    if kind == "reader":
        assert not bao.reads


@pytest.mark.parametrize("kind", ["writer", "reader"])
async def test_recovery_cannot_regress_below_db_pin(client, customers, bao, kind):
    await configure(client, customers[0].id)
    await credential(client, customers[0].id, kind, 1)
    reads = len(bao.reads)
    response = await credential(client, customers[0].id, kind, 2, expected_secret_version=0)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "secret_version_regression"
    assert response.json()["detail"]["pinned_secret_version"] == 1
    assert len(bao.writes) == 1 and len(bao.reads) == reads


async def test_concurrent_recovery_stales_second_request(client, customers, bao):
    await configure(client, customers[0].id)
    await credential(client, customers[0].id, "writer", 1)
    path = repository_service.repository_secret_path(customers[0].id, "test", "writer")
    bao.secrets[path].append({"username": "writer", "token": "orphan-secret"})
    responses = await asyncio.gather(
        credential(client, customers[0].id, "writer", 2, expected_secret_version=2),
        credential(client, customers[0].id, "writer", 2, expected_secret_version=2),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(bao.writes) == 2 and len(bao.secrets[path]) == 3


@pytest.mark.parametrize("legacy", [False, True])
async def test_reader_rejects_writer_token_including_known_legacy_writer(
    client, session, customers, bao, legacy
):
    saved = await configure(client, customers[0].id)
    await credential(client, customers[0].id, "writer", 1)
    repository = await session.get(CustomerClusterRepository, saved["id"])
    if legacy:
        repository.writer_secret_version = None
        await session.commit()
    else:
        # An uncommitted/orphan version must not replace the active pin during this check.
        path = repository_service.repository_secret_path(customers[0].id, "test", "writer")
        bao.secrets[path].append({"username": "writer", "token": "orphan-secret"})
    response = await credential(client, customers[0].id, "reader", 2, token=SECRET)
    assert response.status_code == 409 and "must differ" in response.text
    assert SECRET not in response.text
    assert len(bao.writes) == 1 and bao.reads[-1][1] == (None if legacy else 1)
    await session.refresh(repository)
    assert repository.reader_secret_version is None and repository.version == 2
    assert repository.writer_secret_version == (None if legacy else 1)


async def test_reader_comparison_failure_does_not_write_or_leak(
    client, customers, bao, monkeypatch
):
    await configure(client, customers[0].id)
    await credential(client, customers[0].id, "writer", 1)

    async def unavailable(*args, **kwargs):
        raise OpenBaoError(SECRET)

    monkeypatch.setattr(bao, "read_kv_secret_versioned", unavailable)
    response = await credential(client, customers[0].id, "reader", 2)
    assert response.status_code == 503 and SECRET not in response.text
    assert len(bao.writes) == 1
