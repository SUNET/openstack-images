"""Persisted cluster GitOps settings, readiness and explicit publication intent."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.gitops_source import observe
from app.models import (
    ClusterGitOps,
    Contract,
    CustomerClusterRepository,
    GitOpsOperation,
    TenantCluster,
)
from app.repository_service import get_repository, require_environment


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def cluster_by_slug(slug: str, session: AsyncSession) -> TenantCluster:
    cluster = await session.scalar(
        select(TenantCluster).where(TenantCluster.slug == slug).options(
            selectinload(TenantCluster.contract).selectinload(Contract.customer)
        )
    )
    if cluster is None:
        raise HTTPException(404, "Cluster not found")
    return cluster


async def binding(
    cluster: TenantCluster, settings: Settings, session: AsyncSession, *, attach: bool = False
) -> tuple[CustomerClusterRepository, ClusterGitOps | None]:
    repository = await get_repository(cluster.contract.customer_id, settings, session, lock=attach)
    if repository is None:
        raise HTTPException(409, {
            "code": "repository_unconfigured",
            "message": "Configure the shared customer repository first",
            "customer_id": cluster.contract.customer_id,
        })
    state = await session.get(ClusterGitOps, cluster.id, populate_existing=True)
    if state and (
        state.environment != repository.environment or state.repository_id != repository.id
    ):
        raise HTTPException(409, {
            "code": "repository_binding_conflict",
            "message": "Cluster repository ownership changed; manual migration is required",
        })
    if state is None and attach:
        state = ClusterGitOps(
            cluster_id=cluster.id, repository_id=repository.id,
            environment=repository.environment, version=1, baseline="{}",
        )
        session.add(state)
        await session.flush()
    return repository, state


def operation_view(operation: GitOpsOperation) -> dict[str, Any]:
    payload = json.loads(operation.payload)
    preview = payload.get("preview") or {}
    return {
        "id": operation.id,
        "kind": operation.kind,
        "status": operation.status,
        "error_code": operation.error_code,
        "error_message": operation.error_message,
        "result_commit": operation.result_commit,
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "diff": preview.get("diff") if preview else None,
        "action": preview.get("action"),
        "bases_revision": preview.get("bases_revision"),
        "expected_head": preview.get("expected_head"),
        "validation": preview.get("validation"),
        "source": payload.get("source"),
    }


async def status_view(
    cluster: TenantCluster, settings: Settings, session: AsyncSession
) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    if error := execution_blocker(settings):
        blockers.append(error)
    repository, state = None, None
    try:
        repository, state = await binding(cluster, settings, session)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "configuration_unavailable", "message": str(exc.detail)
        }
        blockers.append({"code": detail["code"], "message": detail["message"]})
    if repository is not None:
        if not repository.writer_secret_version:
            blockers.append({
                "code": "writer_unconfigured", "message": "Store or validate the writer credential"
            })
        elif repository.validation_status != "valid":
            blockers.append({
                "code": "repository_unvalidated", "message": "Validate shared repository access"
            })
    infrastructure = await observe(cluster.slug, settings)
    if infrastructure["status"] != "ready":
        blockers.append({
            "code": f"infrastructure_{infrastructure['status']}",
            "message": infrastructure["message"],
        })
    history_query = select(GitOpsOperation).join(CustomerClusterRepository).where(
        GitOpsOperation.cluster_id == cluster.id,
        CustomerClusterRepository.environment == settings.cluster_environment,
    )
    operations = (await session.scalars(
        history_query
        .order_by(GitOpsOperation.created_at.desc(), GitOpsOperation.id).limit(20)
    )).all()
    active = (await session.scalars(history_query.where(
        GitOpsOperation.status.in_(("queued", "running"))
    ))).all()
    if active:
        blockers.append({
            "code": "operation_active", "message": "A GitOps operation is in progress"
        })
    operations = list({operation.id: operation for operation in [*active, *operations]}.values())
    return {
        "customer_id": cluster.contract.customer_id,
        "environment": state.environment if state else settings.cluster_environment,
        "repository_id": repository.id if repository else None,
        "version": state.version if state else 0,
        "acme_contact": (state.acme_contact if state else None)
        or settings.customer_cluster_acme_contact,
        "reader_installed_version": state.reader_installed_version if state else None,
        "last_commit": state.last_commit if state else None,
        "published_at": state.published_at if state else None,
        "infrastructure": infrastructure,
        "can_preview": not blockers,
        "blockers": blockers,
        "operations": [operation_view(operation) for operation in operations],
    }


async def save_settings(
    cluster: TenantCluster, expected_version: int, acme_contact: str,
    settings: Settings, session: AsyncSession,
) -> ClusterGitOps:
    # Serialize before observing the version, including a first attachment racing another editor.
    await get_repository(cluster.contract.customer_id, settings, session, lock=True)
    _, existing = await binding(cluster, settings, session)
    before = existing.version if existing else 0
    _, state = await binding(cluster, settings, session, attach=True)
    if expected_version != before:
        raise HTTPException(409, "GitOps settings changed; reload before editing")
    if state.acme_contact != acme_contact:
        state.acme_contact = acme_contact
        state.version += 1
    return state


async def enqueue_preview(
    cluster: TenantCluster, adopt: bool, actor: str, settings: Settings, session: AsyncSession
) -> GitOpsOperation:
    repository, state = await binding(cluster, settings, session, attach=True)
    if not repository.writer_secret_version or repository.validation_status != "valid":
        raise HTTPException(409, "Configure and validate shared repository writer access first")
    active = await session.scalar(
        select(GitOpsOperation).where(
            GitOpsOperation.cluster_id == cluster.id,
            GitOpsOperation.status.in_(("queued", "running")),
        )
    )
    if active:
        return active
    await session.execute(update(GitOpsOperation).where(
        GitOpsOperation.cluster_id == cluster.id, GitOpsOperation.status == "preview_ready"
    ).values(status="superseded"))
    operation = GitOpsOperation(
        id=str(uuid4()), cluster_id=cluster.id, repository_id=repository.id,
        kind="preview", status="queued", requested_by_sub=actor,
        payload=json.dumps({
            "repository_version": repository.version,
            "settings_version": state.version,
            "adopt": adopt,
        }),
    )
    session.add(operation)
    await session.flush()
    return operation


async def enqueue_publish(
    cluster: TenantCluster, operation_id: str, settings: Settings, session: AsyncSession,
    actor: str | None = None,
) -> GitOpsOperation:
    repository, state = await binding(cluster, settings, session, attach=True)
    operation = await session.get(GitOpsOperation, operation_id, with_for_update=True)
    if operation is None or operation.cluster_id != cluster.id:
        raise HTTPException(404, "GitOps operation not found for this cluster")
    if operation.repository_id != repository.id:
        raise HTTPException(409, "Operation belongs to another repository")
    if operation.kind == "publish" and operation.status in {"queued", "running", "succeeded"}:
        return operation
    other = await session.scalar(select(GitOpsOperation.id).where(
        GitOpsOperation.cluster_id == cluster.id,
        GitOpsOperation.id != operation.id,
        GitOpsOperation.status.in_(("queued", "running")),
    ).limit(1))
    if other is not None:
        raise HTTPException(409, "Another GitOps operation is active; wait before retrying")
    payload = json.loads(operation.payload)
    if "preview" not in payload or operation.status not in {"preview_ready", "failed"}:
        raise HTTPException(409, "Generate and review a fresh preview before publishing")
    if operation.kind != "publish" and (
        payload["settings_version"] != state.version
        or payload["repository_version"] != repository.version
    ):
        raise HTTPException(409, "Configuration changed after preview; generate a new preview")
    if operation.kind != "publish" and (
        not repository.writer_secret_version or repository.validation_status != "valid"
    ):
        raise HTTPException(409, "Validate repository access before publishing")
    if operation.status == "failed" and operation.kind != "publish":
        raise HTTPException(409, "Generate a successful preview before publishing")
    if operation.kind != "publish":
        payload["approved_by_sub"] = actor or operation.requested_by_sub
        payload["approved_at"] = now().isoformat()
    else:
        payload["retried_by_sub"] = actor or operation.requested_by_sub
        payload["retried_at"] = now().isoformat()
    operation.payload = json.dumps(payload)
    operation.kind = "publish"
    operation.status = "queued"
    operation.error_code = None
    operation.error_message = None
    operation.finished_at = None
    return operation


def execution_blocker(settings: Settings) -> dict[str, str] | None:
    if not settings.gitops_worker_enabled:
        return {
            "code": "worker_disabled", "message": "GitOps execution is disabled in this deployment"
        }
    if (
        settings.cluster_environment not in {"test", "prod"}
        or not settings.managed_cluster_namespace
    ):
        return {
            "code": "worker_unconfigured",
            "message": "Configure the GitOps environment and namespace",
        }
    if not settings.cluster_git_repo_url:
        return {
            "code": "source_unconfigured",
            "message": "Configure the management inventory repository",
        }
    return None


async def get_operation(
    operation_id: str, settings: Settings, session: AsyncSession
) -> GitOpsOperation:
    operation = await session.get(GitOpsOperation, operation_id)
    repository = (
        await session.get(CustomerClusterRepository, operation.repository_id)
        if operation else None
    )
    if repository is None or repository.environment != require_environment(settings):
        raise HTTPException(404, "GitOps operation not found")
    return operation
