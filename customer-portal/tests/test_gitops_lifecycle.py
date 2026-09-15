"""Real PostgreSQL/API/worker orchestration with isolated external dependencies.

Every API request uses the application's registered routes, session authentication,
CSRF middleware and validation handler. Transactions are independent and committed;
worker recovery and advisory locks are exercised across distinct DB connections.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import respx
import yaml
from itsdangerous import TimestampSigner
from kubernetes.client.rest import ApiException
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import customer_gitops, db, gitops_worker, k8s, main, repository_service
from app.config import Settings, get_settings
from app.customer_gitops import CustomerGitOpsError
from app.gitops_service import cluster_by_slug
from app.models import (
    ClusterAccess,
    ClusterAddon,
    ClusterGitOps,
    ClusterRequest,
    ContractPriceOverride,
    ContractRebate,
    CustomerClusterRepository,
    GitOpsOperation,
    KubeconfigIssuance,
    TenantCluster,
)
from tests.test_api_customer_repositories import MemoryBao
from tests.test_gitops_publisher import BASES_URL, REPO_URL, Repositories
from tests.test_gitops_publisher import commit as local_commit
from tests.test_gitops_publisher import git as local_git
from tests.test_gitops_publisher import repos as repos
from tests.test_gitops_source import INVENTORY_COMMIT, NAMESPACE, ManagedSource, existing_cluster
from tests.test_migration_015 import GitOpsDatabase
from tests.test_migration_015 import gitops_database as gitops_database
from tests.test_migration_015 import gitops_postgres as gitops_postgres

WRITER_TOKEN = "lifecycle-writer-secret-never-in-a-response"
READER_TOKEN = "lifecycle-reader-secret-never-in-a-response"
PROVISIONED_AT = datetime(2025, 5, 7, 12, 30)


def manifest_contact(files: dict[str, str], slug: str) -> str:
    issuer = yaml.safe_load(files[f"clusters/{slug}/addons/argocd-ingress/issuer.yaml"])
    return issuer["spec"]["acme"]["email"]


@dataclass
class Publisher:
    """A remote outcome ledger; preview and publish are separate observable effects."""

    previews: list[dict[str, Any]] = field(default_factory=list)
    publications: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    commits: dict[str, str] = field(default_factory=dict)
    preview_failures: list[Exception] = field(default_factory=list)
    publish_failures: list[Exception] = field(default_factory=list)

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        self.previews.append(kwargs)
        if self.preview_failures:
            raise self.preview_failures.pop(0)
        return {
            "files": deepcopy(kwargs["files"]),
            "repo_url": kwargs["repo_url"],
            "bases_url": kwargs["settings"].customer_cluster_bases_url,
            "diff": "--- a/issuer.yaml\n+++ b/issuer.yaml\n+email: noc@example.test\n",
            "action": "adopt" if kwargs["adopt"] else "initialize",
            "expected_head": None,
            "bases_revision": kwargs["settings"].customer_cluster_bases_revision,
            "validation": {"kustomizations": ["argocd-apps"], "envoy_protections": True},
        }

    def publish(self, **kwargs: Any) -> str:
        self.publications.append(deepcopy({key: value for key, value in kwargs.items()
                                         if key not in {"settings", "before_push"}}))
        kwargs["before_push"]()
        if self.publish_failures:
            raise self.publish_failures.pop(0)
        operation_id = kwargs["operation_id"]
        return self.commits.setdefault(
            operation_id, hashlib.sha256(operation_id.encode()).hexdigest()
        )

    def recover(self, **kwargs: Any) -> str | None:
        self.recoveries.append(deepcopy({key: value for key, value in kwargs.items()
                                       if key != "settings"}))
        return self.commits.get(kwargs["operation_id"])


@dataclass
class Lifecycle:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    source: ManagedSource
    publisher: Publisher
    bao: MemoryBao
    cluster: TenantCluster
    client: httpx.AsyncClient
    repo_url: str = REPO_URL

    @property
    def path(self) -> str:
        return f"/api/admin/clusters/{self.cluster.slug}/gitops"

    @property
    def repository_path(self) -> str:
        return f"/api/admin/customers/{self.cluster.contract.customer_id}/cluster-repository"

    async def configure(self, *, reader: bool = True) -> dict[str, Any]:
        response = await self.client.put(self.repository_path, json={
            "repo_url": self.repo_url, "expected_version": 0,
        })
        assert response.status_code == 200, response.text
        repository = response.json()
        for kind, token in (("writer", WRITER_TOKEN), ("reader", READER_TOKEN)):
            if kind == "reader" and not reader:
                continue
            response = await self.client.post(f"{self.repository_path}/credentials/{kind}", json={
                "username": kind, "token": token, "expected_version": repository["version"],
            })
            assert response.status_code == 200, response.text
            assert token not in response.text
            repository = response.json()
        return await self.validate(repository["version"])

    async def validate(self, version: int) -> dict[str, Any]:
        identity_path = urlsplit(self.repo_url).path.lstrip("/").removesuffix(".git")
        with respx.mock() as router:
            identity = router.get("https://forgejo.example.test/api/v1/user").respond(
                200, json={"login": "writer"},
            )
            repository = router.get(
                f"https://forgejo.example.test/api/v1/repos/{identity_path}"
            ).respond(200, json={
                "private": True, "full_name": identity_path, "clone_url": self.repo_url,
                "permissions": {"push": True},
            })
            response = await self.client.post(f"{self.repository_path}/validate", json={
                "expected_version": version,
            })
            assert response.status_code == 200, response.text
            assert identity.called and repository.called
            assert identity.calls.last.request.headers["Authorization"].startswith("token ")
        assert response.json()["validation_status"] == "valid"
        return response.json()

    async def tick(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> bool:
        return await gitops_worker.run_one(sessions or self.sessions, self.settings, self.source)

    async def status(self) -> dict[str, Any]:
        response = await self.client.get(self.path)
        assert response.status_code == 200, response.text
        return response.json()

    async def poll(self, operation_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/admin/gitops-operations/{operation_id}")
        assert response.status_code == 200, response.text
        assert response.json()["id"] == operation_id
        return response.json()

    async def preview(self, *, adopt: bool = False) -> dict[str, Any]:
        response = await self.client.post(f"{self.path}/preview", json={"adopt": adopt})
        assert response.status_code == 202, response.text
        return response.json()

    async def ready_preview(self) -> dict[str, Any]:
        queued = await self.preview()
        assert queued["status"] == "queued"
        assert await self.tick()
        assert (await self.poll(queued["id"]))["status"] == "running"
        assert await self.tick()
        ready = await self.poll(queued["id"])
        assert ready["status"] == "preview_ready", ready
        return ready

    async def approve(self, operation_id: str, *, actor: str | None = None) -> dict[str, Any]:
        headers = {} if actor is None else {
            "Cookie": f"{main._settings.session_cookie_name}={session_cookie(actor)}",
        }
        response = await self.client.post(
            f"{self.path}/publish", json={"operation_id": operation_id}, headers=headers,
        )
        assert response.status_code == 202, response.text
        assert response.json()["id"] == operation_id
        return response.json()

    async def operation(self, operation_id: str) -> GitOpsOperation:
        async with self.sessions() as session:
            result = await session.get(GitOpsOperation, operation_id)
            assert result is not None
            return result

    async def legacy_rows(self) -> dict[str, list[Any]]:
        tables = (
            "customer", "contract", "tenant_cluster", "cluster_access", "kubeconfig_issuance",
            "cluster_request", "cluster_addon", "resource_price", "contract_price_override",
            "contract_rebate",
        )
        async with self.sessions() as session:
            return {table: (await session.scalars(text(
                f"SELECT row_to_json(r) FROM {table} r ORDER BY id"
            ))).all() for table in tables}

    async def second_cluster(self) -> TenantCluster:
        async with self.sessions() as session:
            first = await cluster_by_slug(self.cluster.slug, session)
            cluster = TenantCluster(
                contract=first.contract, name="Second EOSC", slug="eosc-two",
                created_by_sub="admin@test", openbao_mount="kubernetes/eosc-two",
                management_project_resource_name="eosc-second-management",
            )
            session.add(cluster)
            await session.commit()
            self.source.add(cluster)
            return cluster


def session_cookie(actor: str | None) -> str:
    data = {} if actor is None else {"user": {"sub": actor}}
    return TimestampSigner(main._settings.secret_key).sign(
        base64.b64encode(json.dumps(data).encode())
    ).decode()


def test_publication_records_effective_git_contact_for_the_next_preview() -> None:
    files = customer_gitops.render_tree(
        repo_url=REPO_URL, slug="eosc-one", hostname="argocd.eosc-one.example.test",
        ingress_vip="10.42.0.240", interface="ens3", acme_contact="manual@example.test",
        bases_url=BASES_URL,
    )
    state = ClusterGitOps(
        cluster_id=1, repository_id=1, environment="test", version=1,
        acme_contact="old-draft@example.test", baseline="{}",
    )
    operation = GitOpsOperation(
        id=str(uuid4()), cluster_id=1, repository_id=1, kind="publish", status="running",
        requested_by_sub="admin@test", payload="{}",
    )
    gitops_worker.record_publication(operation, state, {
        "files": files, "repo_url": REPO_URL, "bases_url": BASES_URL,
    }, "a" * 40, settings_version=1)

    assert state.acme_contact == "manual@example.test"
    assert state.version == 2
    assert state.last_commit == operation.result_commit == "a" * 40
    assert operation.status == "succeeded"
    assert customer_gitops.render_tree(
        repo_url=REPO_URL, slug="eosc-one", hostname="argocd.eosc-one.example.test",
        ingress_vip="10.42.0.240", interface="ens3", acme_contact=state.acme_contact,
        bases_url=BASES_URL,
    ) == json.loads(state.baseline)


@pytest.fixture
async def lifecycle(
    gitops_database: GitOpsDatabase, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Lifecycle]:
    gitops_database.migrate("015")
    engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "_session_factory", sessions)
    settings = Settings(
        database_url=gitops_database.async_url.render_as_string(hide_password=False),
        cluster_environment="test", managed_cluster_namespace=NAMESPACE,
        cluster_git_repo_url="https://management.example.test/customer-clusters.git",
        gitops_worker_enabled=True,
        customer_repository_origin="https://forgejo.example.test",
        customer_cluster_bases_url=BASES_URL, customer_cluster_bases_revision="b" * 40,
        admin_users=["admin@test"], customer_cluster_acme_contact="noc@example.test",
    )
    cluster = existing_cluster()
    cluster.api_url = "https://api.eosc-one.test:6443"
    cluster.ca_bundle = "pre-existing-ca"
    cluster.worker_groups = 3
    cluster.initial_worker_groups = 2
    cluster.provisioned_at = PROVISIONED_AT
    cluster.backup_project_resource_name = "eosc-backup"
    async with sessions() as session:
        session.add(cluster)
        await session.flush()
        session.add_all([
            ClusterAccess(cluster_id=cluster.id, user_sub="tenant@test", role="customer_admin",
                          granted_by_sub="old-admin@test"),
            KubeconfigIssuance(
                cluster_id=cluster.id, user_sub="tenant@test", label="Existing laptop",
                cert_serial="existing-serial", rolebinding_name="existing-binding",
                cert_group="customer-admins", expires_at=datetime(2027, 1, 1),
            ),
            ClusterAddon(cluster_id=cluster.id, addon_type="jupyterhub",
                         enabled_at=PROVISIONED_AT, enabled_by_sub="old-admin@test"),
            ClusterRequest(
                cluster_id=cluster.id, request_type="resize", status="applied",
                payload='{"worker_groups":3,"previous_worker_groups":2}',
                requested_by_sub="tenant@test", applied_by_sub="old-admin@test",
                applied_at=PROVISIONED_AT, note="Preserve billing evidence",
            ),
            ContractPriceOverride(
                contract_id=cluster.contract_id, resource_type="cluster_setup_fee",
                unit_price=Decimal("1731.55"),
            ),
            ContractRebate(contract_id=cluster.contract_id, rebate_percent=Decimal("11.25")),
        ])
        await session.commit()
    source = ManagedSource()
    source.add(cluster)
    publisher, bao = Publisher(), MemoryBao()
    monkeypatch.setattr(k8s, "_api", source)
    monkeypatch.setattr(repository_service, "get_openbao", lambda: bao)
    monkeypatch.setattr(gitops_worker, "prepare_preview", publisher.prepare)
    monkeypatch.setattr(gitops_worker, "publish_preview", publisher.publish)
    monkeypatch.setattr(gitops_worker, "recover_preview", publisher.recover)

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with db.session_factory()() as session:
            yield session

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url=main._BASE_ORIGIN,
        headers={"Origin": main._BASE_ORIGIN},
        cookies={main._settings.session_cookie_name: session_cookie("admin@test")},
    ) as client:
        harness = Lifecycle(
            db.session_factory(), settings, source, publisher, bao, cluster, client
        )
        monkeypatch.setattr(main.app, "dependency_overrides", {
            **main.app.dependency_overrides, db.get_session: request_session,
            get_settings: lambda: harness.settings,
        })
        monkeypatch.setattr(main.app.state, "cluster_git_backend", source, raising=False)
        monkeypatch.setattr(main.app.state, "git_backend", Mock(spec=[]), raising=False)
        try:
            yield harness
        finally:
            await engine.dispose()


async def test_existing_live_cluster_without_gitops_configuration_shows_configure_blocker(
    lifecycle: Lifecycle,
) -> None:
    before = await lifecycle.legacy_rows()
    for _ in range(2):
        status = await lifecycle.status()
        assert status["version"] == 0 and status["repository_id"] is None
        assert status["infrastructure"]["status"] == "ready"
        assert status["infrastructure"]["inventory_commit"] == INVENTORY_COMMIT
        assert not status["can_preview"] and status["operations"] == []
        assert [blocker["code"] for blocker in status["blockers"]] == ["repository_unconfigured"]
        assert "Configure" in status["blockers"][0]["message"]
        assert status["last_commit"] is status["published_at"] is None
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ClusterGitOps)) == 0
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation)) == 0
    assert await lifecycle.legacy_rows() == before
    assert not lifecycle.bao.reads and not lifecycle.bao.writes
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications


async def test_binding_and_saving_live_cluster_does_not_recreate_reset_access_or_billing(
    lifecycle: Lifecycle,
) -> None:
    before = await lifecycle.legacy_rows()
    repository = await lifecycle.configure()
    response = await lifecycle.client.put(lifecycle.path, json={
        "expected_version": 0, "acme_contact": "eosc-noc@example.test",
    })
    assert response.status_code == 200, response.text
    version = response.json()["version"]
    assert version > 0
    status = await lifecycle.status()
    assert status["version"] == version and status["repository_id"] == repository["id"]
    assert status["acme_contact"] == "eosc-noc@example.test" and status["can_preview"]
    unchanged = await lifecycle.client.put(lifecycle.path, json={
        "expected_version": version, "acme_contact": "eosc-noc@example.test",
    })
    assert unchanged.status_code == 200 and unchanged.json()["version"] == version
    stale = await lifecycle.client.put(lifecycle.path, json={
        "expected_version": 0, "acme_contact": "stale@example.test",
    })
    assert stale.status_code == 409
    response = await lifecycle.client.get(lifecycle.repository_path)
    assert response.json()["clusters"] == [{
        "slug": "eosc-one", "name": lifecycle.cluster.name, "reader_installed_version": None,
    }]
    assert await lifecycle.legacy_rows() == before
    assert not await lifecycle.tick()
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications


async def test_preview_and_explicit_publish_are_durable_distinct_stages_with_one_stable_id(
    lifecycle: Lifecycle, caplog: pytest.LogCaptureFixture,
) -> None:
    before = await lifecycle.legacy_rows()
    await lifecycle.configure()
    credential_reads = len(lifecycle.bao.reads)
    queued = await lifecycle.preview(adopt=True)
    operation_id = queued["id"]
    assert str(UUID(operation_id)) == operation_id
    assert queued["kind"] == "preview" and queued["status"] == "queued"
    assert queued["started_at"] is None and queued["result_commit"] is None
    duplicates = await asyncio.gather(lifecycle.preview(), lifecycle.preview())
    assert all(item["id"] == operation_id for item in duplicates)
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications
    assert await lifecycle.tick()
    running = await lifecycle.poll(operation_id)
    assert running["status"] == "running" and running["started_at"] is not None
    assert running["finished_at"] is None and not lifecycle.publisher.previews
    status = await lifecycle.status()
    assert not status["can_preview"]
    assert "operation_active" in {item["code"] for item in status["blockers"]}
    assert await lifecycle.tick()
    ready = await lifecycle.poll(operation_id)
    assert ready["status"] == "preview_ready" and ready["finished_at"] is not None
    assert ready["action"] == "adopt" and ready["diff"].startswith("---")
    assert ready["validation"]["envoy_protections"] is True
    assert ready["source"]["inventory_commit"] == INVENTORY_COMMIT
    assert ready["source"]["hostname"] == "argocd.eosc-one.operator.test"
    assert ready["source"]["ingress_vip"] == "10.42.0.240"
    assert lifecycle.publisher.previews[0]["adopt"] is True
    assert lifecycle.publisher.previews[0]["baseline"] == {}
    assert not await lifecycle.tick()
    assert (await lifecycle.status())["operations"][0] == ready
    assert not lifecycle.publisher.publications
    approvals = await asyncio.gather(
        lifecycle.approve(operation_id), lifecycle.approve(operation_id)
    )
    assert all(item["id"] == operation_id and item["status"] == "queued" for item in approvals)
    assert all(item["kind"] == "publish" for item in approvals)
    assert await lifecycle.tick()
    assert (await lifecycle.approve(operation_id))["status"] == "running"
    assert not lifecycle.publisher.publications
    assert await lifecycle.tick()
    succeeded = await lifecycle.poll(operation_id)
    assert succeeded["status"] == "succeeded" and succeeded["error_code"] is None
    assert succeeded["result_commit"] == lifecycle.publisher.commits[operation_id]
    state = await lifecycle.status()
    assert state["last_commit"] == succeeded["result_commit"] and state["published_at"] is not None
    assert state["operations"] == [succeeded]
    operation = await lifecycle.operation(operation_id)
    async with lifecycle.sessions() as session:
        saved = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert json.loads(saved.baseline) == json.loads(operation.payload)["preview"]["files"]
        assert saved.last_commit == operation.result_commit
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation)) == 1
        raw_rows = (await session.scalars(text(
            "SELECT row_to_json(o)::text FROM gitops_operation o UNION ALL "
            "SELECT row_to_json(s)::text FROM cluster_gitops s UNION ALL "
            "SELECT row_to_json(r)::text FROM customer_cluster_repository r"
        ))).all()
    for token in (WRITER_TOKEN, READER_TOKEN):
        assert token not in json.dumps(state) + str(raw_rows) + caplog.text
    assert (await lifecycle.approve(operation_id))["status"] == "succeeded"
    assert not await lifecycle.tick() and len(lifecycle.publisher.publications) == 1
    customer_id = lifecycle.cluster.contract.customer_id
    writer_path = f"kv/data/customer-cluster-repositories/{customer_id}/test/writer"
    assert lifecycle.bao.reads[credential_reads:] == [(writer_path, 1)] * 2
    assert await lifecycle.legacy_rows() == before


@pytest.mark.parametrize("stage", ["queued", "running", "failed"])
async def test_unbuilt_preview_has_null_diff_for_the_ui_loading_contract(
    lifecycle: Lifecycle, stage: str,
) -> None:
    await lifecycle.configure()
    operation = await lifecycle.preview()
    if stage != "queued":
        assert await lifecycle.tick()
    if stage == "failed":
        lifecycle.publisher.preview_failures.append(
            CustomerGitOpsError("Unavailable", "git_failed")
        )
        assert await lifecycle.tick()
    view = await lifecycle.poll(operation["id"])
    assert view["status"] == stage
    assert view["diff"] is None, "An absent preview must not appear as a reviewed empty diff"


async def change_configuration(lifecycle: Lifecycle, kind: str) -> None:
    if kind == "settings":
        status = await lifecycle.status()
        response = await lifecycle.client.put(lifecycle.path, json={
            "expected_version": status["version"], "acme_contact": "changed@example.test",
        })
        assert response.status_code == 200, response.text
    else:
        repository = (await lifecycle.client.get(lifecycle.repository_path)).json()
        response = await lifecycle.client.post(
            f"{lifecycle.repository_path}/credentials/{kind}", json={
                "expected_version": repository["version"], "username": kind,
                "token": f"rotated-{kind}-secret",
            },
        )
        assert response.status_code == 200, response.text
        # Restoring validation must not make a stale version of the preview usable.
        await lifecycle.validate(response.json()["version"])


@pytest.mark.parametrize("kind", ["settings", "writer", "reader"])
@pytest.mark.parametrize("approved", [False, True])
async def test_configuration_and_reader_rotation_stale_preview_before_or_after_approval(
    lifecycle: Lifecycle, kind: str, approved: bool,
) -> None:
    await lifecycle.configure()
    operation = await lifecycle.ready_preview()
    if approved:
        await lifecycle.approve(operation["id"])
    await change_configuration(lifecycle, kind)
    if approved:
        assert await lifecycle.tick()
        assert await lifecycle.tick()
        failed = await lifecycle.poll(operation["id"])
        assert failed["status"] == "conflict" and failed["error_code"] == "configuration_changed"
    else:
        response = await lifecycle.client.post(f"{lifecycle.path}/publish", json={
            "operation_id": operation["id"],
        })
        assert response.status_code == 409 and "changed" in response.text.lower()
        assert (await lifecycle.poll(operation["id"]))["status"] == "preview_ready"
    assert not lifecycle.publisher.publications
    assert (await lifecycle.status())["last_commit"] is None


@pytest.mark.parametrize("change", ["uid", "generation", "inventory_commit"])
async def test_changed_operator_snapshot_after_review_is_a_conflict(
    lifecycle: Lifecycle, change: str,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    document = lifecycle.source.documents[lifecycle.cluster.slug]
    if change == "uid":
        document["metadata"]["uid"] = "replacement-resource"
    elif change == "generation":
        document["metadata"]["generation"] += 1
        document["status"]["observedGeneration"] += 1
    else:
        document["status"]["inventoryCommit"] = "c" * 40
        lifecycle.source.snapshots[lifecycle.cluster.slug, "c" * 40] = deepcopy(
            lifecycle.source.snapshots[lifecycle.cluster.slug, INVENTORY_COMMIT]
        )
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(preview["id"])
    assert failed["status"] == "conflict" and failed["error_code"] == "source_changed"
    assert not lifecycle.publisher.publications


@pytest.mark.parametrize("failure,readiness,error", [
    (403, "forbidden", "forbidden"), (404, "missing", "missing"),
    (503, "unavailable", "unavailable"), (None, "pending", "inventory_pending"),
])
async def test_readiness_and_worker_distinguish_forbidden_missing_and_pending_sources(
    lifecycle: Lifecycle, failure: int | None, readiness: str, error: str,
) -> None:
    await lifecycle.configure()
    if failure is None:
        lifecycle.source.documents[lifecycle.cluster.slug]["status"]["observedGeneration"] = 6
    else:
        api_error = ApiException(status=failure, reason=WRITER_TOKEN)
        api_error.body = READER_TOKEN
        lifecycle.source.error = api_error
    state = await lifecycle.status()
    assert state["infrastructure"]["status"] == readiness and not state["can_preview"]
    assert {item["code"] for item in state["blockers"]} == {f"infrastructure_{readiness}"}
    queued = await lifecycle.preview()
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(queued["id"])
    assert failed["status"] == "failed" and failed["error_code"] == error
    assert not lifecycle.source.reads and not lifecycle.publisher.previews
    assert WRITER_TOKEN not in json.dumps(failed) and READER_TOKEN not in json.dumps(state)


async def test_reader_installation_is_per_cluster_version_attestation_not_publication(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    response = await lifecycle.client.post(
        f"{lifecycle.path}/reader-installed", json={"version": 1}
    )
    assert response.status_code == 200 and response.json() == {"reader_installed_version": 1}
    second = await lifecycle.second_cluster()
    second_state = (await lifecycle.client.get(f"/api/admin/clusters/{second.slug}/gitops")).json()
    assert second_state["reader_installed_version"] is None
    await change_configuration(lifecycle, "reader")
    assert (await lifecycle.status())["reader_installed_version"] == 1
    stale = await lifecycle.client.post(f"{lifecycle.path}/reader-installed", json={"version": 1})
    assert stale.status_code == 409
    current = await lifecycle.client.post(
        f"{lifecycle.path}/reader-installed", json={"version": 2}
    )
    assert current.status_code == 200
    assert (await lifecycle.status())["reader_installed_version"] == 2
    assert not await lifecycle.tick()
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications


@pytest.mark.parametrize("actor,status", [(None, 401), ("tenant@test", 403)])
async def test_cluster_admin_grants_do_not_authorize_any_gitops_or_repository_api(
    lifecycle: Lifecycle, actor: str | None, status: int,
) -> None:
    await lifecycle.configure()
    operation = await lifecycle.ready_preview()
    before = await lifecycle.legacy_rows()
    effects = (
        len(lifecycle.source.api_calls), len(lifecycle.bao.reads), len(lifecycle.bao.writes)
    )
    routes = [
        ("GET", lifecycle.path, None),
        ("PUT", lifecycle.path, {"expected_version": 1, "acme_contact": "noc@example.test"}),
        ("POST", f"{lifecycle.path}/preview", {}),
        ("POST", f"{lifecycle.path}/publish", {"operation_id": operation["id"]}),
        ("POST", f"{lifecycle.path}/reader-installed", {"version": 1}),
        ("GET", f"/api/admin/gitops-operations/{operation['id']}", None),
        ("GET", lifecycle.repository_path, None),
        ("PUT", lifecycle.repository_path, {"expected_version": 3, "repo_url": REPO_URL}),
        ("POST", f"{lifecycle.repository_path}/credentials/writer", {
            "expected_version": 3, "username": "writer", "token": WRITER_TOKEN,
        }),
        ("POST", f"{lifecycle.repository_path}/credentials/reader", {
            "expected_version": 3, "username": "reader", "token": READER_TOKEN,
        }),
        ("POST", f"{lifecycle.repository_path}/validate", {"expected_version": 3}),
    ]
    for method, path, body in routes:
        response = await lifecycle.client.request(method, path, json=body, headers={
            "Cookie": f"{main._settings.session_cookie_name}={session_cookie(actor)}",
        })
        assert response.status_code == status, (path, response.text)
        assert WRITER_TOKEN not in response.text and READER_TOKEN not in response.text
    assert effects == (
        len(lifecycle.source.api_calls), len(lifecycle.bao.reads), len(lifecycle.bao.writes)
    )
    assert await lifecycle.legacy_rows() == before
    assert (await lifecycle.poll(operation["id"]))["status"] == "preview_ready"


async def test_operation_ids_are_cluster_bound_and_polling_is_environment_scoped(
    lifecycle: Lifecycle,
) -> None:
    repository = await lifecycle.configure()
    operation = await lifecycle.ready_preview()
    second = await lifecycle.second_cluster()
    wrong_cluster = await lifecycle.client.post(
        f"/api/admin/clusters/{second.slug}/gitops/publish", json={"operation_id": operation["id"]}
    )
    assert wrong_cluster.status_code == 404
    missing = str(uuid4())
    missing_path = f"/api/admin/gitops-operations/{missing}"
    assert (await lifecycle.client.get(missing_path)).status_code == 404
    assert (await lifecycle.client.post(f"{lifecycle.path}/publish", json={
        "operation_id": missing,
    })).status_code == 404
    async with lifecycle.sessions() as session:
        prod = CustomerClusterRepository(
            customer_id=lifecycle.cluster.contract.customer_id, environment="prod",
            repo_url="https://forgejo.example.test/customer/prod.git", writer_username="writer",
        )
        session.add(prod)
        await session.flush()
        session.add(GitOpsOperation(
            id=missing, cluster_id=lifecycle.cluster.id, repository_id=prod.id, kind="preview",
            status="running", requested_by_sub="admin@test",
        ))
        await session.commit()
    assert (await lifecycle.client.get(missing_path)).status_code == 404
    assert not await lifecycle.tick(), "A test worker must not consume a prod operation"
    assert (await lifecycle.poll(operation["id"]))["status"] == "preview_ready"
    assert missing not in {item["id"] for item in (await lifecycle.status())["operations"]}, (
        "Cluster history must not expose operations that the polling endpoint refuses to return"
    )
    assert repository["environment"] == "test"


@pytest.mark.parametrize("conflict", ["environment", "repository"])
async def test_persisted_binding_conflicts_block_edits_and_worker_publication(
    lifecycle: Lifecycle, conflict: str,
) -> None:
    repository = await lifecycle.configure()
    queued = await lifecycle.preview()
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        if conflict == "environment":
            state.environment = "prod"
        else:
            other = CustomerClusterRepository(
                customer_id=lifecycle.cluster.contract.customer_id, environment="prod",
                repo_url="https://forgejo.example.test/customer/prod.git", writer_username="",
            )
            session.add(other)
            await session.flush()
            state.repository_id = other.id
        await session.commit()
    status = await lifecycle.status()
    assert not status["can_preview"]
    assert "repository_binding_conflict" in {item["code"] for item in status["blockers"]}
    for method, suffix, body in (
        ("PUT", "", {"expected_version": 1, "acme_contact": "noc@example.test"}),
        ("POST", "/preview", {}),
        ("POST", "/publish", {"operation_id": queued["id"]}),
    ):
        response = await lifecycle.client.request(method, lifecycle.path + suffix, json=body)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "repository_binding_conflict"
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(queued["id"])
    assert failed["status"] == "conflict" and failed["error_code"] == "repository_binding_conflict"
    assert not lifecycle.source.reads and not lifecycle.publisher.publications
    assert (await lifecycle.operation(queued["id"])).repository_id == repository["id"]


async def test_transient_publish_retry_reuses_exact_preview_source_and_operation(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    operation_id = preview["id"]
    payload = json.loads((await lifecycle.operation(operation_id)).payload)
    lifecycle.publisher.publish_failures.append(
        CustomerGitOpsError("Push unavailable", "push_failed")
    )
    await lifecycle.approve(operation_id)
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(operation_id)
    assert failed["status"] == "failed" and failed["error_code"] == "push_failed"
    assert failed["diff"] == preview["diff"] and failed["result_commit"] is None
    assert not await lifecycle.tick(), "Failed jobs require an explicit retry"
    assert (await lifecycle.status())["last_commit"] is None
    retry = await lifecycle.approve(operation_id)
    assert retry["status"] == "queued" and retry["error_code"] is retry["finished_at"] is None
    retried_payload = json.loads((await lifecycle.operation(operation_id)).payload)
    assert {key: retried_payload[key] for key in payload} == payload
    assert await lifecycle.tick() and await lifecycle.tick()
    assert (await lifecycle.poll(operation_id))["status"] == "succeeded"
    assert len(lifecycle.publisher.previews) == 1
    assert lifecycle.publisher.publications[0] == lifecycle.publisher.publications[1]
    assert len(lifecycle.publisher.commits) == 1


async def test_failed_previews_cannot_publish_and_new_preview_supersedes_only_reviewable_jobs(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    lifecycle.publisher.preview_failures.append(
        CustomerGitOpsError("No inventory", "inventory_pending")
    )
    failed = await lifecycle.preview()
    assert await lifecycle.tick() and await lifecycle.tick()
    response = await lifecycle.client.post(
        f"{lifecycle.path}/publish", json={"operation_id": failed["id"]}
    )
    assert response.status_code == 409
    first_ready = await lifecycle.ready_preview()
    second = await lifecycle.preview()
    assert second["id"] not in {failed["id"], first_ready["id"]}
    assert (await lifecycle.poll(first_ready["id"]))["status"] == "superseded"
    assert (await lifecycle.poll(failed["id"]))["status"] == "failed"
    response = await lifecycle.client.post(f"{lifecycle.path}/publish", json={
        "operation_id": first_ready["id"],
    })
    assert response.status_code == 409 and not lifecycle.publisher.publications


@pytest.mark.parametrize("failure", ["credential", "unexpected"])
async def test_worker_dependency_errors_do_not_persist_or_echo_secret_exception_bodies(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    await lifecycle.configure()
    if failure == "credential":
        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            from app.openbao_client import OpenBaoError

            raise OpenBaoError(WRITER_TOKEN)

        monkeypatch.setattr(lifecycle.bao, "read_kv_secret_versioned", unavailable)
    else:
        lifecycle.publisher.preview_failures.append(RuntimeError(WRITER_TOKEN))
    queued = await lifecycle.preview()
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(queued["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == (
        "repository_credentials_unavailable" if failure == "credential" else "operation_failed"
    )
    operation = await lifecycle.operation(queued["id"])
    assert WRITER_TOKEN not in (
        json.dumps(failed) + operation.payload + operation.error_message + caplog.text
    )


@pytest.mark.parametrize("suffix,method,body", [
    ("", "PUT", {"expected_version": True, "acme_contact": WRITER_TOKEN}),
    ("", "PUT", [WRITER_TOKEN, READER_TOKEN]),
    ("/preview", "POST", {"adopt": {"token": WRITER_TOKEN}}),
    ("/preview", "POST", {"token": WRITER_TOKEN}),
    ("/publish", "POST", {"operation_id": WRITER_TOKEN}),
    ("/reader-installed", "POST", {"version": WRITER_TOKEN}),
])
async def test_registered_validation_handler_redacts_entire_422_response(
    lifecycle: Lifecycle, suffix: str, method: str, body: Any,
) -> None:
    response = await lifecycle.client.request(method, lifecycle.path + suffix, json=body)
    assert response.status_code == 422, response.text
    assert WRITER_TOKEN not in response.text and READER_TOKEN not in response.text
    assert response.json()["detail"]
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])
    assert not lifecycle.bao.reads and not lifecycle.bao.writes and not lifecycle.source.api_calls


@pytest.mark.parametrize("body", [
    {"expected_version": 0, "username": [], "token": WRITER_TOKEN},
    {"expected_version": 0, "username": "writer", "token": {"secret": WRITER_TOKEN}},
    [WRITER_TOKEN, READER_TOKEN],
])
async def test_repository_422_never_echoes_tokens_or_the_whole_request(
    lifecycle: Lifecycle, body: Any,
) -> None:
    response = await lifecycle.client.post(
        f"{lifecycle.repository_path}/credentials/writer", json=body
    )
    assert response.status_code == 422
    assert WRITER_TOKEN not in response.text and READER_TOKEN not in response.text
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])
    assert not lifecycle.bao.reads and not lifecycle.bao.writes


async def test_invalid_json_body_is_redacted_in_422_not_returned_as_parser_context(
    lifecycle: Lifecycle,
) -> None:
    response = await lifecycle.client.post(
        f"{lifecycle.repository_path}/credentials/writer",
        content=f'{{"token":"{WRITER_TOKEN}","username":"writer",',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert WRITER_TOKEN not in response.text
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])
    assert not lifecycle.bao.reads and not lifecycle.bao.writes


@dataclass
class ThreadGate:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def wrap(self, function: Callable[..., Any]) -> Callable[..., Any]:
        def blocked(**kwargs: Any) -> Any:
            self.entered.set()
            try:
                assert self.release.wait(10), "Test did not release the publisher thread"
                return function(**kwargs)
            finally:
                self.finished.set()

        return blocked

    async def wait_for_entry(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 5), "Worker never entered Git work"


async def repository_lock_available(lifecycle: Lifecycle, repository_id: int) -> bool:
    async with lifecycle.sessions() as session, session.begin():
        return bool(await session.scalar(text(
            "SELECT pg_try_advisory_xact_lock(hashtext('customer-repository'), :id)"
        ), {"id": repository_id}))


async def test_two_workers_serialize_two_clusters_in_the_same_customer_repository(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = await lifecycle.configure()
    second = await lifecycle.second_cluster()
    first = await lifecycle.preview()
    response = await lifecycle.client.post(
        f"/api/admin/clusters/{second.slug}/gitops/preview", json={}
    )
    assert response.status_code == 202
    second_operation = response.json()
    assert second_operation["id"] != first["id"]
    assert await lifecycle.tick()
    gate = ThreadGate()
    monkeypatch.setattr(gitops_worker, "prepare_preview", gate.wrap(lifecycle.publisher.prepare))
    worker = asyncio.create_task(lifecycle.tick())
    try:
        await gate.wait_for_entry()
        assert not await repository_lock_available(lifecycle, repository["id"])
        assert not await asyncio.wait_for(lifecycle.tick(), timeout=2)
        assert (await lifecycle.poll(first["id"]))["status"] == "running"
        assert (await lifecycle.poll(second_operation["id"]))["status"] == "queued"
        assert not lifecycle.publisher.previews
    finally:
        gate.release.set()
        result = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=5)
    assert result == [True] and gate.finished.is_set()
    assert await repository_lock_available(lifecycle, repository["id"])
    assert (await lifecycle.poll(first["id"]))["status"] == "preview_ready"
    assert await lifecycle.tick() and await lifecycle.tick()
    assert (await lifecycle.poll(second_operation["id"]))["status"] == "preview_ready"
    assert len(lifecycle.publisher.previews) == 2
    assert not lifecycle.publisher.publications


async def test_repository_edit_waits_for_publisher_lock_then_invalidates_pending_preview(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = await lifecycle.configure()
    queued = await lifecycle.preview()
    assert await lifecycle.tick()
    gate = ThreadGate()
    monkeypatch.setattr(gitops_worker, "prepare_preview", gate.wrap(lifecycle.publisher.prepare))
    worker = asyncio.create_task(lifecycle.tick())
    rotation = None
    try:
        await gate.wait_for_entry()
        writes_before = len(lifecycle.bao.writes)
        rotation = asyncio.create_task(lifecycle.client.post(
            f"{lifecycle.repository_path}/credentials/reader", json={
                "expected_version": repository["version"], "username": "reader",
                "token": "replacement-reader-token",
            },
        ))
        # Observe an actual blocked PostgreSQL lock request, rather than infer it from a sleep.
        async with asyncio.timeout(5):
            while True:
                async with lifecycle.sessions() as session:
                    waiting = await session.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                    ))
                if waiting:
                    break
                await asyncio.sleep(0.01)
        assert not rotation.done() and len(lifecycle.bao.writes) == writes_before
    finally:
        gate.release.set()
        tasks = [worker] + ([rotation] if rotation is not None else [])
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    assert results[0] is True and results[1].status_code == 200
    assert gate.finished.is_set()
    assert len(lifecycle.bao.writes) == writes_before + 1
    preview = await lifecycle.poll(queued["id"])
    assert preview["status"] == "preview_ready"
    response = await lifecycle.client.post(f"{lifecycle.path}/publish", json={
        "operation_id": queued["id"],
    })
    assert response.status_code == 409 and not lifecycle.publisher.publications


@pytest.mark.parametrize("kind", ["preview", "publish"])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_waits_for_git_thread_before_releasing_repository_lock(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, kind: str, cancellations: int,
) -> None:
    repository = await lifecycle.configure()
    if kind == "publish":
        operation = await lifecycle.ready_preview()
        await lifecycle.approve(operation["id"])
    else:
        operation = await lifecycle.preview()
    assert await lifecycle.tick()
    gate = ThreadGate()
    method = lifecycle.publisher.prepare if kind == "preview" else lifecycle.publisher.publish
    target = "publish_preview" if kind == "publish" else "prepare_preview"
    monkeypatch.setattr(gitops_worker, target, gate.wrap(method))
    worker = asyncio.create_task(lifecycle.tick())
    try:
        await gate.wait_for_entry()
        for _ in range(cancellations):
            worker.cancel()
            await asyncio.sleep(0)
        done, _ = await asyncio.wait({worker}, timeout=0.05)
        assert not gate.finished.is_set()
        assert not await repository_lock_available(lifecycle, repository["id"]), (
            "Cancellation released the transaction while the Git thread was running"
        )
        assert not done, "The worker must wait for its Git thread to finish"
        assert not await asyncio.wait_for(lifecycle.tick(), timeout=2)
    finally:
        gate.release.set()
        result = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=5)
        assert await asyncio.to_thread(gate.finished.wait, 5)
    assert isinstance(result[0], asyncio.CancelledError)
    assert await repository_lock_available(lifecycle, repository["id"])
    # Cancelled work cannot commit a terminal DB state; another replica resumes running intent.
    assert (await lifecycle.poll(operation["id"]))["status"] == "running"
    assert await lifecycle.tick()
    recovered = await lifecycle.poll(operation["id"])
    assert recovered["status"] == ("preview_ready" if kind == "preview" else "succeeded")
    if kind == "publish":
        assert len(lifecycle.publisher.publications) == 1 and len(lifecycle.publisher.commits) == 1
        assert len(lifecycle.publisher.recoveries) == 2


async def test_cancellation_remains_cancellation_when_background_publisher_raises() -> None:
    gate = ThreadGate()

    def fail() -> None:
        raise CustomerGitOpsError("Remote transport failed", "push_failed")

    task = asyncio.create_task(gitops_worker.thread_call(gate.wrap(fail)))
    try:
        await gate.wait_for_entry()
        task.cancel()
        await asyncio.sleep(0)
    finally:
        gate.release.set()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert gate.finished.is_set()
    assert isinstance(result[0], asyncio.CancelledError), (
        "A publisher exception after cancellation must not turn cancelled intent "
        "into a committed failure"
    )


@pytest.mark.parametrize("change", ["none", "writer", "settings", "source", "all"])
async def test_real_local_publish_recovers_after_db_commit_failure_without_duplicate_commit(
    lifecycle: Lifecycle, repos: Repositories, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, change: str,
) -> None:
    lifecycle.settings = replace(
        lifecycle.settings,
        customer_cluster_bases_url=repos.settings.customer_cluster_bases_url,
        customer_cluster_bases_revision=repos.settings.customer_cluster_bases_revision,
    )
    publication_settings = lifecycle.settings
    monkeypatch.setattr(gitops_worker, "prepare_preview", customer_gitops.prepare_preview)
    monkeypatch.setattr(gitops_worker, "recover_preview", customer_gitops.recover_preview)
    publications: list[str] = []

    def publish(**kwargs: Any) -> str:
        commit = customer_gitops.publish_preview(**kwargs)
        publications.append(commit)
        return commit

    monkeypatch.setattr(gitops_worker, "publish_preview", publish)
    before = await lifecycle.legacy_rows()
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    assert preview["validation"]["envoy_protections"] is True
    assert len(preview["validation"]["kustomizations"]) == 6
    assert local_git(repos.customer, "show-ref", check=False) == ""
    assert not await lifecycle.tick() and not publications
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick()
    running = await lifecycle.operation(preview["id"])
    approved_payload = json.loads(running.payload)
    approved_files = approved_payload["preview"]["files"]
    approved_contact = manifest_contact(approved_files, lifecycle.cluster.slug)
    assert approved_contact == "noc@example.test"
    pending_draft: tuple[str, int] | None = None
    engine = lifecycle.sessions.kw["bind"]

    def fail_commit(connection: Any) -> None:
        assert publications, "Failure must happen after the remote publication finished"
        raise OperationalError("COMMIT", None, RuntimeError("synthetic database connection loss"))

    event.listen(engine.sync_engine, "commit", fail_commit)
    try:
        with pytest.raises(OperationalError):
            await lifecycle.tick()
    finally:
        event.remove(engine.sync_engine, "commit", fail_commit)
    assert len(publications) == 1
    commit = publications[0]
    assert commit == local_git(repos.customer, "rev-parse", "main")
    assert local_git(repos.customer, "rev-list", "--count", "main") == "1"
    current = await lifecycle.operation(preview["id"])
    assert current.status == "running" and current.result_commit is None
    assert current.payload == running.payload and current.started_at == running.started_at
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert state.baseline == "{}" and state.last_commit is state.published_at is None
        assert state.acme_contact is None and state.version == approved_payload["settings_version"]
    if change in {"writer", "all"}:
        repository = (await lifecycle.client.get(lifecycle.repository_path)).json()
        response = await lifecycle.client.post(
            f"{lifecycle.repository_path}/credentials/writer", json={
                "expected_version": repository["version"], "username": "writer",
                "token": "rotated-recovery-writer-secret",
            },
        )
        assert response.status_code == 200
        assert response.json()["validation_status"] == "unvalidated"
        assert response.json()["writer_secret_version"] == 2
    if change in {"settings", "all"}:
        await change_configuration(lifecycle, "settings")
        async with lifecycle.sessions() as session:
            state = await session.get(ClusterGitOps, lifecycle.cluster.id)
            pending_draft = state.acme_contact, state.version
        assert pending_draft == ("changed@example.test", approved_payload["settings_version"] + 1)
        lifecycle.settings = replace(
            lifecycle.settings,
            customer_cluster_bases_url="https://changed.example.test/bases/new.git",
            customer_cluster_bases_revision="f" * 40, customer_cluster_node_interface="ens9",
        )
    if change in {"source", "all"}:
        lifecycle.source.documents[lifecycle.cluster.slug]["metadata"]["generation"] += 1
        lifecycle.source.error = ApiException(status=403, reason="Source no longer readable")
    source_reads = len(lifecycle.source.reads), len(lifecycle.source.api_calls)
    with monkeypatch.context() as recovery_patch:
        recovery_patch.setattr(gitops_worker, "load_snapshot", Mock(side_effect=AssertionError(
            "Recovery must not depend on reading current infrastructure"
        )))
        recovery_patch.setattr(gitops_worker, "assert_fresh", Mock(side_effect=AssertionError(
            "Read-only recovery must never enter the new-push readiness gate"
        )))
        # A completely fresh connection pool has no in-memory knowledge of the previous worker.
        recovery_engine = create_async_engine(lifecycle.settings.database_url)
        recovery_sessions = async_sessionmaker(recovery_engine, expire_on_commit=False)
        try:
            assert await lifecycle.tick(recovery_sessions)
            assert not await lifecycle.tick(recovery_sessions)
        finally:
            await recovery_engine.dispose()
    recovered = await lifecycle.poll(preview["id"])
    assert recovered["status"] == "succeeded" and recovered["result_commit"] == commit
    assert publications == [commit]
    assert source_reads == (len(lifecycle.source.reads), len(lifecycle.source.api_calls))
    expected_pin = 2 if change in {"writer", "all"} else 1
    assert lifecycle.bao.reads[-1][1] == expected_pin
    assert local_git(repos.customer, "rev-list", "--count", "main") == "1"
    assert f"Customer-GitOps-Operation: {preview['id']}" in local_git(
        repos.customer, "log", "-1", "--format=%B"
    )
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert json.loads(state.baseline) == approved_files
        assert manifest_contact(json.loads(state.baseline), lifecycle.cluster.slug) == (
            approved_contact
        )
        assert state.last_commit == commit and state.published_at is not None
        if pending_draft is not None:
            assert (state.acme_contact, state.version) == pending_draft
        else:
            assert state.acme_contact == approved_contact
            assert state.version == approved_payload["settings_version"] + 1
    assert (await lifecycle.approve(preview["id"]))["status"] == "succeeded"
    assert not await lifecycle.tick() and len(publications) == 1
    if pending_draft is not None:
        # Re-enable validation to preview B against the recovered A publication receipt.
        lifecycle.settings = publication_settings
        lifecycle.source.error = None
        document = lifecycle.source.documents[lifecycle.cluster.slug]
        document["status"]["observedGeneration"] = document["metadata"]["generation"]
        repository = (await lifecycle.client.get(lifecycle.repository_path)).json()
        if repository["validation_status"] != "valid":
            await lifecycle.validate(repository["version"])
        status = await lifecycle.status()
        assert (status["acme_contact"], status["version"]) == pending_draft
        pending_contact, pending_version = pending_draft
        next_preview = await lifecycle.ready_preview()
        assert next_preview["id"] != preview["id"] and next_preview["action"] == "update"
        assert next_preview["expected_head"] == commit
        next_payload = json.loads((await lifecycle.operation(next_preview["id"])).payload)
        assert next_payload["settings_version"] == pending_version
        assert manifest_contact(next_payload["preview"]["files"], lifecycle.cluster.slug) == (
            pending_contact
        )
        assert any(line.startswith("+") and f"email: {pending_contact}" in line
                   for line in next_preview["diff"].splitlines())
        assert any(line.startswith("-") and f"email: {approved_contact}" in line
                   for line in next_preview["diff"].splitlines())
        async with lifecycle.sessions() as session:
            state = await session.get(ClusterGitOps, lifecycle.cluster.id)
            assert (state.acme_contact, state.version) == pending_draft
            assert json.loads(state.baseline) == approved_files and state.last_commit == commit
        assert (await lifecycle.operation(preview["id"])).payload == running.payload
        assert local_git(repos.customer, "rev-parse", "main") == commit
        assert local_git(repos.customer, "rev-list", "--count", "main") == "1"
        assert publications == [commit]
    assert await lifecycle.legacy_rows() == before
    assert WRITER_TOKEN not in json.dumps(recovered) + caplog.text


@pytest.mark.parametrize("receipt", ["default", "manual-noop", "manual-update"])
async def test_real_publication_adopts_effective_contact_and_invalidates_stale_editors(
    lifecycle: Lifecycle, repos: Repositories, monkeypatch: pytest.MonkeyPatch, receipt: str,
) -> None:
    lifecycle.settings = replace(
        lifecycle.settings,
        customer_cluster_bases_url=repos.settings.customer_cluster_bases_url,
        customer_cluster_bases_revision=repos.settings.customer_cluster_bases_revision,
    )
    monkeypatch.setattr(gitops_worker, "prepare_preview", customer_gitops.prepare_preview)
    monkeypatch.setattr(gitops_worker, "recover_preview", customer_gitops.recover_preview)
    monkeypatch.setattr(gitops_worker, "publish_preview", customer_gitops.publish_preview)
    await lifecycle.configure()
    initial_status = await lifecycle.status()
    assert initial_status["version"] == 0
    async with lifecycle.sessions() as session:
        assert await session.get(ClusterGitOps, lifecycle.cluster.id) is None
    preview = await lifecycle.ready_preview()
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert state.acme_contact is None and state.version == 1
    effective_contact = "noc@example.test"
    if receipt != "default":
        await lifecycle.approve(preview["id"])
        assert await lifecycle.tick() and await lifecycle.tick()
        assert (await lifecycle.poll(preview["id"]))["status"] == "succeeded"
        initial_receipt = await lifecycle.status()
        assert initial_receipt["acme_contact"] == effective_contact
        assert initial_receipt["version"] == 2
        effective_contact = "manual-customer-noc@example.test"
        issuer_path = f"clusters/{lifecycle.cluster.slug}/addons/argocd-ingress/issuer.yaml"
        manual_files = repos.tree(
            slug=lifecycle.cluster.slug, hostname=preview["source"]["hostname"],
            ingress_vip=preview["source"]["ingress_vip"], acme_contact=effective_contact,
        )
        local_commit(repos.customer, {issuer_path: manual_files[issuer_path]})
        if receipt == "manual-update":
            lifecycle.settings = replace(
                lifecycle.settings, customer_cluster_node_interface="ens4"
            )
        preview = await lifecycle.ready_preview()
        assert preview["action"] == ("noop" if receipt == "manual-noop" else "update")
        async with lifecycle.sessions() as session:
            state = await session.get(ClusterGitOps, lifecycle.cluster.id)
            assert state.acme_contact == "noc@example.test" and state.version == 2
            assert manifest_contact(json.loads(state.baseline), lifecycle.cluster.slug) == (
                "noc@example.test"
            )
    reviewed = json.loads((await lifecycle.operation(preview["id"])).payload)
    original_version = reviewed["settings_version"]
    assert manifest_contact(reviewed["preview"]["files"], lifecycle.cluster.slug) == (
        effective_contact
    )
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick() and await lifecycle.tick()
    published = await lifecycle.poll(preview["id"])
    assert published["status"] == "succeeded"
    receipt_version = original_version + 1
    status = await lifecycle.status()
    assert (status["acme_contact"], status["version"]) == (effective_contact, receipt_version)
    stale_versions = {original_version}
    if receipt == "default":
        # Both a pre-attachment editor and the version-1 editor saw no persisted contact.
        stale_versions.add(initial_status["version"])
    for stale_version in stale_versions:
        response = await lifecycle.client.put(lifecycle.path, json={
            "expected_version": stale_version, "acme_contact": "stale-editor@example.test",
        })
        assert response.status_code == 409, response.text
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert (state.acme_contact, state.version) == (effective_contact, receipt_version)
        assert json.loads(state.baseline) == reviewed["preview"]["files"]
        assert state.last_commit == published["result_commit"]
    assert (await lifecycle.approve(preview["id"]))["status"] == "succeeded"
    next_preview = await lifecycle.ready_preview()
    assert next_preview["action"] == "noop" and next_preview["diff"] == ""
    next_payload = json.loads((await lifecycle.operation(next_preview["id"])).payload)
    assert next_payload["settings_version"] == receipt_version
    assert manifest_contact(next_payload["preview"]["files"], lifecycle.cluster.slug) == (
        effective_contact
    )
    assert local_git(repos.customer, "rev-parse", "main") == published["result_commit"]
    expected_commits = {"default": "1", "manual-noop": "2", "manual-update": "3"}
    assert local_git(repos.customer, "rev-list", "--count", "main") == expected_commits[receipt]
    await lifecycle.approve(next_preview["id"])
    assert await lifecycle.tick() and await lifecycle.tick()
    unchanged = await lifecycle.poll(next_preview["id"])
    assert unchanged["status"] == "succeeded"
    assert unchanged["result_commit"] == published["result_commit"]
    status = await lifecycle.status()
    assert (status["acme_contact"], status["version"]) == (effective_contact, receipt_version)
    assert local_git(repos.customer, "rev-list", "--count", "main") == expected_commits[receipt]


async def test_fresh_worker_resumes_running_preview_using_its_persisted_intent(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    operation = await lifecycle.preview(adopt=True)
    assert await lifecycle.tick()
    running = await lifecycle.operation(operation["id"])
    assert running.status == "running" and not lifecycle.publisher.previews
    engine = create_async_engine(lifecycle.settings.database_url)
    try:
        new_sessions = async_sessionmaker(engine, expire_on_commit=False)
        assert await lifecycle.tick(new_sessions)
    finally:
        await engine.dispose()
    ready = await lifecycle.poll(operation["id"])
    assert ready["status"] == "preview_ready" and ready["action"] == "adopt"
    assert (await lifecycle.operation(operation["id"])).started_at == running.started_at
    assert len(lifecycle.publisher.previews) == 1 and not lifecycle.publisher.publications


async def test_worker_loop_drains_queued_preview_and_stops_without_implicit_publication(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await lifecycle.configure()
    queued = await lifecycle.preview()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def prepare(**kwargs: Any) -> dict[str, Any]:
        preview = lifecycle.publisher.prepare(**kwargs)
        loop.call_soon_threadsafe(stop.set)
        return preview

    monkeypatch.setattr(gitops_worker, "prepare_preview", prepare)
    await asyncio.wait_for(gitops_worker.run_worker(
        stop, lifecycle.sessions, lifecycle.settings, lifecycle.source,
    ), timeout=5)
    ready = await lifecycle.poll(queued["id"])
    assert ready["status"] == "preview_ready" and ready["started_at"] is not None
    assert len(lifecycle.publisher.previews) == 1 and not lifecycle.publisher.publications
    assert not await lifecycle.tick()


async def test_worker_loop_retains_running_publish_when_commit_fails_and_recovers_on_restart(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick()
    stop = asyncio.Event()
    engine = lifecycle.sessions.kw["bind"]

    def fail_commit(connection: Any) -> None:
        assert preview["id"] in lifecycle.publisher.commits
        stop.set()
        raise OperationalError("COMMIT", None, RuntimeError(WRITER_TOKEN))

    event.listen(engine.sync_engine, "commit", fail_commit)
    try:
        await asyncio.wait_for(gitops_worker.run_worker(
            stop, lifecycle.sessions, lifecycle.settings, lifecycle.source,
        ), timeout=5)
    finally:
        event.remove(engine.sync_engine, "commit", fail_commit)
    assert (await lifecycle.poll(preview["id"]))["status"] == "running"
    assert "worker dependency failure" in caplog.text and WRITER_TOKEN not in caplog.text
    restart_stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def recover(**kwargs: Any) -> str | None:
        commit = lifecycle.publisher.recover(**kwargs)
        loop.call_soon_threadsafe(restart_stop.set)
        return commit

    monkeypatch.setattr(gitops_worker, "recover_preview", recover)
    await asyncio.wait_for(gitops_worker.run_worker(
        restart_stop, lifecycle.sessions, lifecycle.settings, lifecycle.source,
    ), timeout=5)
    recovered = await lifecycle.poll(preview["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["result_commit"] == lifecycle.publisher.commits[preview["id"]]
    assert len(lifecycle.publisher.publications) == 1 and len(lifecycle.publisher.commits) == 1
    assert len(lifecycle.publisher.recoveries) == 2


async def test_first_settings_attachment_serializes_optimistic_versions(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    responses = await asyncio.gather(*(
        lifecycle.client.put(lifecycle.path, json={
            "expected_version": 0, "acme_contact": contact,
        }) for contact in ("first@example.test", "second@example.test")
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 200)
    status = await lifecycle.status()
    assert status["acme_contact"] == ("first@example.test", "second@example.test")[winner]
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ClusterGitOps)) == 1
    assert not await lifecycle.tick() and not lifecycle.publisher.publications


async def test_preview_requires_pinned_valid_writer_and_rejected_enqueue_does_not_attach(
    lifecycle: Lifecycle,
) -> None:
    response = await lifecycle.client.put(lifecycle.repository_path, json={
        "repo_url": REPO_URL, "expected_version": 0,
    })
    assert response.status_code == 200
    status = await lifecycle.status()
    assert not status["can_preview"]
    assert {item["code"] for item in status["blockers"]} == {"writer_unconfigured"}
    refused = await lifecycle.client.post(f"{lifecycle.path}/preview", json={})
    assert refused.status_code == 409
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ClusterGitOps)) == 0
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation)) == 0
    stored = await lifecycle.client.post(f"{lifecycle.repository_path}/credentials/writer", json={
        "expected_version": 1, "username": "writer", "token": WRITER_TOKEN,
    })
    assert stored.status_code == 200
    status = await lifecycle.status()
    assert {item["code"] for item in status["blockers"]} == {"repository_unvalidated"}
    refused = await lifecycle.client.post(f"{lifecycle.path}/preview", json={})
    assert refused.status_code == 409
    await lifecycle.validate(stored.json()["version"])
    assert (await lifecycle.status())["can_preview"]
    assert (await lifecycle.preview())["status"] == "queued"
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications


@pytest.mark.parametrize("section,changes,expected", [
    ("metadata", {"deletionTimestamp": "2026-09-15T17:00:00Z"}, "deleting"),
    ("spec", {"suspend": True}, "suspended"),
    ("status", {"phase": "Suspended"}, "suspended"),
    ("status", {"phase": "Failed"}, "failed"),
])
async def test_api_reports_explicit_deleting_suspended_and_failed_readiness(
    lifecycle: Lifecycle, section: str, changes: dict[str, Any], expected: str,
) -> None:
    await lifecycle.configure()
    lifecycle.source.documents[lifecycle.cluster.slug][section].update(changes)
    status = await lifecycle.status()
    assert status["infrastructure"]["status"] == expected and not status["can_preview"]
    assert {item["code"] for item in status["blockers"]} == {f"infrastructure_{expected}"}
    queued = await lifecycle.preview()
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.poll(queued["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "inventory_pending"
    assert not lifecycle.source.reads
    assert not lifecycle.publisher.previews and not lifecycle.publisher.publications


@pytest.mark.parametrize("change", ["generation", "deleting", "suspended", "failed", "forbidden"])
async def test_source_change_inside_publisher_is_checked_by_the_pre_push_callback(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick()
    before = len(lifecycle.source.api_calls)

    def publish(**kwargs: Any) -> str:
        document = lifecycle.source.documents[lifecycle.cluster.slug]
        if change == "generation":
            document["metadata"]["generation"] += 1
            document["status"]["observedGeneration"] += 1
        elif change == "deleting":
            document["metadata"]["deletionTimestamp"] = "2026-09-15T17:00:00Z"
        elif change == "suspended":
            document["spec"]["suspend"] = True
        elif change == "failed":
            document["status"]["phase"] = "Failed"
        else:
            lifecycle.source.error = ApiException(status=403, reason=WRITER_TOKEN)
        return lifecycle.publisher.publish(**kwargs)

    monkeypatch.setattr(gitops_worker, "publish_preview", publish)
    assert await lifecycle.tick()
    failed = await lifecycle.poll(preview["id"])
    assert failed["status"] == "conflict" and failed["error_code"] == "source_changed"
    assert len(lifecycle.source.api_calls) == before + 2
    assert len(lifecycle.publisher.publications) == 1 and not lifecycle.publisher.commits
    assert failed["result_commit"] is None and WRITER_TOKEN not in json.dumps(failed)
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert state.baseline == "{}" and state.last_commit is state.published_at is None


async def test_real_local_publisher_cannot_push_source_that_changes_during_kustomize_validation(
    lifecycle: Lifecycle, repos: Repositories, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle.settings = replace(
        lifecycle.settings,
        customer_cluster_bases_url=repos.settings.customer_cluster_bases_url,
        customer_cluster_bases_revision=repos.settings.customer_cluster_bases_revision,
    )
    monkeypatch.setattr(gitops_worker, "prepare_preview", customer_gitops.prepare_preview)
    monkeypatch.setattr(gitops_worker, "recover_preview", customer_gitops.recover_preview)
    monkeypatch.setattr(gitops_worker, "publish_preview", customer_gitops.publish_preview)
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick()
    original = customer_gitops.validate_kustomize
    validations: list[dict[str, Any]] = []

    def validate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        validations.append(result)
        lifecycle.source.documents[lifecycle.cluster.slug]["metadata"]["generation"] += 1
        return result

    monkeypatch.setattr(customer_gitops, "validate_kustomize", validate)
    assert await lifecycle.tick()
    failed = await lifecycle.poll(preview["id"])
    assert failed["status"] == "conflict" and failed["error_code"] == "source_changed"
    assert len(validations) == 1 and validations[0]["envoy_protections"] is True
    assert local_git(repos.customer, "show-ref", check=False) == ""
    assert failed["result_commit"] is None
    async with lifecycle.sessions() as session:
        state = await session.get(ClusterGitOps, lifecycle.cluster.id)
        assert state.baseline == "{}" and state.last_commit is state.published_at is None


@pytest.mark.parametrize("changes,code", [
    ({"gitops_worker_enabled": False}, "worker_disabled"),
    ({"cluster_git_repo_url": ""}, "source_unconfigured"),
    ({"cluster_environment": ""}, "worker_unconfigured"),
    ({"cluster_environment": "staging"}, "worker_unconfigured"),
    ({"managed_cluster_namespace": ""}, "worker_unconfigured"),
    ({}, "source_unavailable"),
])
async def test_disabled_execution_returns_503_without_stranding_preview_or_publish_intent(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any], code: str,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    original = await lifecycle.operation(preview["id"])
    reads = len(lifecycle.source.api_calls), len(lifecycle.bao.reads)
    lifecycle.settings = replace(lifecycle.settings, **changes)
    if not changes:
        monkeypatch.setattr(main.app.state, "cluster_git_backend", None)
    for suffix, body in (("preview", {}), ("publish", {"operation_id": preview["id"]})):
        response = await lifecycle.client.post(f"{lifecycle.path}/{suffix}", json=body)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == code
    assert reads == (len(lifecycle.source.api_calls), len(lifecycle.bao.reads))
    unchanged = await lifecycle.operation(preview["id"])
    assert unchanged.kind == "preview" and unchanged.status == "preview_ready"
    assert unchanged.payload == original.payload
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation)) == 1
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation).where(
            GitOpsOperation.status.in_(("queued", "running"))
        )) == 0
    assert not lifecycle.publisher.publications
    status = await lifecycle.status()
    assert not status["can_preview"], "The UI must not offer a queue action that is disabled"
    assert code in {item["code"] for item in status["blockers"]}


async def test_approval_and_retry_audit_records_preserve_the_original_approver(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.settings = replace(
        lifecycle.settings, admin_users=["admin@test", "approver@test", "retrier@test"]
    )
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    operation_id = preview["id"]
    original = await lifecycle.operation(operation_id)
    assert original.requested_by_sub == "admin@test"
    original_payload = json.loads(original.payload)
    assert "approved_by_sub" not in original_payload and "approved_at" not in original_payload
    before = datetime.now(UTC).replace(tzinfo=None)
    await lifecycle.approve(operation_id, actor="approver@test")
    approved = json.loads((await lifecycle.operation(operation_id)).payload)
    assert approved["approved_by_sub"] == "approver@test"
    assert before <= datetime.fromisoformat(approved["approved_at"]) <= (
        datetime.now(UTC).replace(tzinfo=None)
    )
    assert {key: approved[key] for key in original_payload} == original_payload
    await lifecycle.approve(operation_id, actor="retrier@test")
    assert json.loads((await lifecycle.operation(operation_id)).payload) == approved
    lifecycle.publisher.publish_failures.append(CustomerGitOpsError("Retry push", "push_failed"))
    assert await lifecycle.tick()
    await lifecycle.approve(operation_id, actor="admin@test")
    assert json.loads((await lifecycle.operation(operation_id)).payload) == approved
    assert await lifecycle.tick()
    assert (await lifecycle.poll(operation_id))["status"] == "failed"
    await lifecycle.approve(operation_id, actor="retrier@test")
    retried = json.loads((await lifecycle.operation(operation_id)).payload)
    assert {key: retried[key] for key in approved} == approved
    assert retried["retried_by_sub"] == "retrier@test"
    assert datetime.fromisoformat(retried["retried_at"]) >= datetime.fromisoformat(
        approved["approved_at"]
    )
    await lifecycle.approve(operation_id, actor="admin@test")
    assert json.loads((await lifecycle.operation(operation_id)).payload) == retried
    assert await lifecycle.tick() and await lifecycle.tick()
    succeeded = await lifecycle.operation(operation_id)
    assert succeeded.status == "succeeded" and succeeded.requested_by_sub == "admin@test"
    assert json.loads(succeeded.payload) == retried


@pytest.mark.parametrize("active_status", ["queued", "running"])
async def test_retry_of_failed_publish_cannot_compete_with_another_active_operation(
    lifecycle: Lifecycle, active_status: str,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    lifecycle.publisher.publish_failures.append(CustomerGitOpsError("Retry push", "push_failed"))
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick() and await lifecycle.tick()
    failed = await lifecycle.operation(preview["id"])
    assert failed.status == "failed" and failed.kind == "publish"
    other = await lifecycle.preview()
    if active_status == "running":
        assert await lifecycle.tick()
    for _ in range(2):
        response = await lifecycle.client.post(f"{lifecycle.path}/publish", json={
            "operation_id": preview["id"],
        })
        assert response.status_code == 409 and "active" in response.text.lower()
    assert (await lifecycle.poll(other["id"]))["status"] == active_status
    unchanged = await lifecycle.operation(preview["id"])
    assert unchanged.status == "failed" and unchanged.payload == failed.payload
    assert unchanged.error_code == failed.error_code
    assert unchanged.finished_at == failed.finished_at
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation).where(
            GitOpsOperation.status.in_(("queued", "running"))
        )) == 1
    assert len(lifecycle.publisher.publications) == 1 and not lifecycle.publisher.commits


async def test_failed_publish_retry_racing_new_preview_still_has_only_one_active_operation(
    lifecycle: Lifecycle,
) -> None:
    await lifecycle.configure()
    preview = await lifecycle.ready_preview()
    lifecycle.publisher.publish_failures.append(CustomerGitOpsError("Retry push", "push_failed"))
    await lifecycle.approve(preview["id"])
    assert await lifecycle.tick() and await lifecycle.tick()
    responses = await asyncio.gather(
        lifecycle.client.post(f"{lifecycle.path}/preview", json={}),
        lifecycle.client.post(f"{lifecycle.path}/publish", json={"operation_id": preview["id"]}),
    )
    assert responses[0].status_code == 202
    assert responses[1].status_code in (202, 409)
    async with lifecycle.sessions() as session:
        active = (await session.scalars(select(GitOpsOperation).where(
            GitOpsOperation.status.in_(("queued", "running"))
        ))).all()
        assert len(active) == 1
        accepted_ids = {
            response.json()["id"] for response in responses if response.status_code == 202
        }
        assert accepted_ids == {active[0].id}


@pytest.mark.parametrize("active_status", ["queued", "running"])
async def test_active_operation_older_than_last_twenty_remains_visible_and_blocks_new_intent(
    lifecycle: Lifecycle, active_status: str,
) -> None:
    repository = await lifecycle.configure()
    active = await lifecycle.preview()
    if active_status == "running":
        assert await lifecycle.tick()
    history_ids = [str(uuid4()) for _ in range(23)]
    async with lifecycle.sessions() as session:
        operation = await session.get(GitOpsOperation, active["id"])
        operation.created_at = datetime(2025, 1, 1)
        session.add_all([
            GitOpsOperation(
                id=operation_id, cluster_id=lifecycle.cluster.id, repository_id=repository["id"],
                kind="preview", status="failed", requested_by_sub="admin@test",
                created_at=datetime(2026, 1, 1) + timedelta(seconds=index),
                error_code="old_failure", error_message="Historical failure",
            ) for index, operation_id in enumerate(history_ids)
        ])
        await session.commit()
    status = await lifecycle.status()
    visible_ids = [operation["id"] for operation in status["operations"]]
    assert len(visible_ids) == len(set(visible_ids)) == 21
    assert set(visible_ids) == {active["id"], *history_ids[-20:]}
    assert not status["can_preview"]
    assert "operation_active" in {blocker["code"] for blocker in status["blockers"]}
    assert (await lifecycle.poll(active["id"]))["status"] == active_status
    assert (await lifecycle.preview())["id"] == active["id"]
    async with lifecycle.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitOpsOperation)) == 24


async def seed_preview_jobs(
    lifecycle: Lifecycle, repository: dict[str, Any], jobs: list[tuple[str, datetime]],
) -> None:
    """Persist one queued intent per cluster, including recovery-style timestamps."""
    async with lifecycle.sessions() as session:
        original = await cluster_by_slug(lifecycle.cluster.slug, session)
        for index, (operation_id, created_at) in enumerate(jobs):
            slug = f"{original.slug}-job-{index}"
            cluster = TenantCluster(
                contract=original.contract, name=slug, slug=slug,
                created_by_sub="admin@test", openbao_mount=f"kubernetes/{slug}",
                management_project_resource_name=f"management-{slug}",
            )
            session.add(cluster)
            await session.flush()
            session.add_all([
                ClusterGitOps(cluster_id=cluster.id, repository_id=repository["id"],
                              environment="test", version=1),
                GitOpsOperation(
                    id=operation_id, cluster_id=cluster.id, repository_id=repository["id"],
                    kind="preview", status="queued", requested_by_sub="admin@test",
                    created_at=created_at, payload=json.dumps({
                        "repository_version": repository["version"], "settings_version": 1,
                        "adopt": False,
                    }),
                ),
            ])
            lifecycle.source.add(cluster)
        await session.commit()


async def test_busy_repository_backlog_does_not_starve_a_different_customer_repository(
    lifecycle: Lifecycle,
) -> None:
    busy_repository = await lifecycle.configure()
    backlog = [(str(uuid4()), datetime(2025, 1, 1) + timedelta(seconds=index))
               for index in range(35)]
    await seed_preview_jobs(lifecycle, busy_repository, backlog)
    other_cluster = existing_cluster("other-one")
    other_cluster.contract.customer.name = "Other customer"
    other_cluster.contract.customer.domain = "other.test"
    other_cluster.contract.contract_number = "OTHER-2026"
    other_cluster.management_project_resource_name = "other-management"
    async with lifecycle.sessions() as session:
        session.add(other_cluster)
        await session.commit()
    lifecycle.source.add(other_cluster)
    other = replace(
        lifecycle, cluster=other_cluster, repo_url="https://forgejo.example.test/other/clusters.git"
    )
    await other.configure(reader=False)
    free_job = await other.preview()
    async with lifecycle.sessions() as lock_session, lock_session.begin():
        await lock_session.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('customer-repository'), :id)"
        ), {"id": busy_repository["id"]})
        assert await asyncio.wait_for(lifecycle.tick(), timeout=3)
        assert (await other.poll(free_job["id"]))["status"] == "running"
        assert await asyncio.wait_for(lifecycle.tick(), timeout=3)
        assert (await other.poll(free_job["id"]))["status"] == "preview_ready"
        async with lifecycle.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GitOpsOperation).where(
                GitOpsOperation.repository_id == busy_repository["id"],
                GitOpsOperation.status == "queued",
            )) == len(backlog)
        assert len(lifecycle.publisher.previews) == 1
        assert lifecycle.publisher.previews[0]["repo_url"] == other.repo_url
    assert not lifecycle.publisher.publications


async def test_worker_selects_fifo_with_id_tiebreak_and_finishes_running_head_before_next_job(
    lifecycle: Lifecycle,
) -> None:
    repository = await lifecycle.configure()
    oldest, later_high_id, later_low_id = (str(UUID(int=value)) for value in (9, 2, 1))
    await seed_preview_jobs(lifecycle, repository, [
        (later_high_id, datetime(2025, 1, 2)),
        (oldest, datetime(2025, 1, 1)),
        (later_low_id, datetime(2025, 1, 2)),
    ])
    expected_order = [oldest, later_low_id, later_high_id]
    for index, operation_id in enumerate(expected_order):
        assert await lifecycle.tick()
        assert (await lifecycle.poll(operation_id))["status"] == "running"
        for following in expected_order[index + 1:]:
            assert (await lifecycle.poll(following))["status"] == "queued"
        assert await lifecycle.tick()
        assert (await lifecycle.poll(operation_id))["status"] == "preview_ready"
        assert len(lifecycle.publisher.previews) == index + 1
    assert not await lifecycle.tick() and not lifecycle.publisher.publications
