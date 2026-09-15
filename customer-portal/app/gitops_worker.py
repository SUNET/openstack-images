"""PostgreSQL-backed GitOps worker; a process restart resumes committed intent.

    queued -> running -> preview_ready
                         (explicit approval) -> queued -> running -> succeeded

External work holds transaction-scoped repository and operation locks. Running
intent is committed separately first: process death releases locks but preserves
the job for another replica. Git publication itself is independently idempotent.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cluster_git_backend import ClusterGitBackend
from app.config import Settings
from app.customer_gitops import (
    CustomerGitOpsError,
    prepare_preview,
    publish_preview,
    recover_preview,
    render_tree,
    validate_tree,
)
from app.gitops_service import cluster_by_slug, now
from app.gitops_source import assert_fresh, load_snapshot
from app.models import ClusterGitOps, CustomerClusterRepository, GitOpsOperation, TenantCluster
from app.repository_service import writer_credentials

logger = logging.getLogger(__name__)
CONFLICTS = {
    "stale_preview", "manual_conflict", "configuration_changed", "source_changed",
    "repository_binding_conflict", "incompatible_adoption", "adoption_required",
}


async def thread_call(function: Callable[..., Any], **kwargs: Any) -> Any:
    """Do not release a repository lock while cancelled Git work is still running."""
    task = asyncio.create_task(asyncio.to_thread(function, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        with suppress(BaseException):
            task.result()
        raise asyncio.CancelledError()
    return task.result()


def record_publication(
    operation: GitOpsOperation, state: ClusterGitOps, preview: dict, commit: str,
    settings_version: int,
) -> None:
    effective = validate_tree(
        preview["files"], repo_url=preview["repo_url"], bases_url=preview["bases_url"]
    )
    # Preserve the effective Git value after a compatible manual edit, rather
    # than treating the old draft/default as a new requested change next time.
    if state.version == settings_version and state.acme_contact != effective.acme_contact:
        state.acme_contact = effective.acme_contact
        state.version += 1
    state.baseline = json.dumps(preview["files"])
    state.last_commit = commit
    state.published_at = now()
    operation.result_commit = commit
    operation.status = "succeeded"
    operation.finished_at = now()


async def execute_operation(
    operation: GitOpsOperation, session: AsyncSession, settings: Settings,
    backend: ClusterGitBackend | None,
) -> None:
    repository = await session.get(CustomerClusterRepository, operation.repository_id)
    cluster_row = await session.get(TenantCluster, operation.cluster_id)
    state = await session.get(ClusterGitOps, operation.cluster_id)
    if repository is None or cluster_row is None or state is None:
        raise CustomerGitOpsError("GitOps configuration disappeared", "configuration_changed")
    cluster = await cluster_by_slug(cluster_row.slug, session)
    if (
        repository.environment != settings.cluster_environment
        or state.environment != repository.environment
        or state.repository_id != repository.id
        or cluster.contract.customer_id != repository.customer_id
    ):
        raise CustomerGitOpsError(
            "Cluster repository ownership changed", "repository_binding_conflict"
        )
    payload = json.loads(operation.payload)
    username, token = await writer_credentials(repository, settings)
    if operation.kind == "publish":
        # A prior push may have succeeded before a crash. Recover its receipt
        # read-only even if the operator/credential configuration has since changed.
        recovered = await thread_call(
            recover_preview, repo_url=repository.repo_url, username=username, token=token,
            preview=payload["preview"], operation_id=operation.id, settings=settings,
        )
        if recovered is not None:
            record_publication(
                operation, state, payload["preview"], recovered, payload["settings_version"]
            )
            return
    if (
        payload["repository_version"] != repository.version
        or payload["settings_version"] != state.version
        or repository.validation_status != "valid"
    ):
        raise CustomerGitOpsError(
            "Configuration changed; validate access and prepare a new preview",
            "configuration_changed",
        )
    snapshot = await load_snapshot(cluster, backend, settings)
    source = snapshot.as_dict()
    if operation.kind == "preview":
        files = render_tree(
            repo_url=repository.repo_url, slug=cluster.slug, hostname=snapshot.hostname,
            ingress_vip=snapshot.ingress_vip, interface=snapshot.interface,
            acme_contact=state.acme_contact or settings.customer_cluster_acme_contact,
            bases_url=settings.customer_cluster_bases_url,
        )
        preview = await thread_call(
            prepare_preview, repo_url=repository.repo_url, username=username, token=token,
            files=files, baseline=json.loads(state.baseline), settings=settings,
            adopt=payload["adopt"],
        )
        payload.update({"source": source, "preview": preview})
        operation.payload = json.dumps(payload)
        operation.status = "preview_ready"
    elif operation.kind == "publish":
        if source != payload["source"]:
            raise CustomerGitOpsError(
                "Operator input changed after preview; prepare a fresh preview", "source_changed"
            )
        commit = await thread_call(
            publish_preview, repo_url=repository.repo_url, username=username, token=token,
            preview=payload["preview"], operation_id=operation.id, settings=settings,
            before_push=lambda: assert_fresh(snapshot, settings),
        )
        record_publication(
            operation, state, payload["preview"], commit, payload["settings_version"]
        )
    else:
        raise CustomerGitOpsError("Unknown GitOps operation type", "invalid_operation")
    operation.finished_at = now()


async def run_one(
    sessions: async_sessionmaker[AsyncSession], settings: Settings,
    backend: ClusterGitBackend | None,
) -> bool:
    """Advance one operation, serialized with repository configuration and all replicas."""
    async with sessions() as session:
        candidates = (await session.execute(
            select(GitOpsOperation.repository_id)
            .join(CustomerClusterRepository)
            .where(
                GitOpsOperation.status.in_(("queued", "running")),
                CustomerClusterRepository.environment == settings.cluster_environment,
            ).group_by(GitOpsOperation.repository_id)
            .order_by(func.min(GitOpsOperation.created_at)).limit(30)
        )).scalars().all()
    for repository_id in candidates:
        async with sessions() as session, session.begin():
            # Lock order is repository -> operation, shared with the approval endpoint.
            acquired = await session.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtext('customer-repository'), :id)"
            ), {"id": repository_id})
            if not acquired:
                continue
            operation = await session.scalar(
                select(GitOpsOperation).where(
                    GitOpsOperation.repository_id == repository_id,
                    GitOpsOperation.status.in_(("queued", "running")),
                ).order_by(GitOpsOperation.created_at, GitOpsOperation.id)
                .limit(1).with_for_update(skip_locked=True)
            )
            if operation is None:
                continue
            if operation.status == "queued":
                operation.status = "running"
                operation.started_at = now()
                return True
            try:
                await execute_operation(operation, session, settings, backend)
            except CustomerGitOpsError as exc:
                operation.error_code = exc.code or "publication_failed"
                operation.error_message = str(exc)[:512]
                operation.status = "conflict" if exc.code in CONFLICTS else "failed"
                operation.finished_at = now()
            except HTTPException as exc:
                operation.error_code = "repository_credentials_unavailable"
                # Do not persist credential-service exception bodies.
                operation.error_message = (
                    "Repository credentials are unavailable; "
                    "validate or replace the shared credential"
                )
                operation.status = "failed"
                operation.finished_at = now()
                logger.warning(
                    "GitOps credential failure operation=%s status=%s",
                    operation.id, exc.status_code,
                )
            except Exception as exc:
                operation.error_code = "operation_failed"
                operation.error_message = "GitOps operation failed; retry or prepare a new preview"
                operation.status = "failed"
                operation.finished_at = now()
                logger.error(
                    "GitOps failure operation=%s type=%s", operation.id, type(exc).__name__
                )
            return True
    return False


async def run_worker(
    stop: asyncio.Event, sessions: async_sessionmaker[AsyncSession], settings: Settings,
    backend: ClusterGitBackend | None,
) -> None:
    while not stop.is_set():
        try:
            worked = await run_one(sessions, settings, backend)
        except Exception as exc:
            # A failed DB commit leaves running intent intact for idempotent recovery.
            logger.error("GitOps worker dependency failure type=%s", type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass
