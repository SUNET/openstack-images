"""Tenant cluster management endpoints.

- /api/admin/clusters/*  : SUNET-admin-only CRUD on the cluster registry.
- /api/clusters/*        : member-facing list/get + access management.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import kubeconfig_service
from app.auth import (
    get_current_user,
    is_sunet_admin,
    require_admin,
    require_cluster_access,
)
from app.config import Settings, get_settings
from app.db import get_session
from app.git_backend import GitBackend
from app.models import (
    ClusterAccess,
    ClusterAddon,
    Contract,
    TenantCluster,
)
from app.schemas import (
    ClusterAccessRequest,
    ClusterAccessResponse,
    ClusterResponse,
    CreateClusterRequest,
    UpdateClusterRequest,
    _size_label,
)

logger = logging.getLogger(__name__)


admin_router = APIRouter(prefix="/api/admin/clusters", tags=["admin-clusters"])
member_router = APIRouter(prefix="/api/clusters", tags=["clusters"])


async def _active_addons(cluster_id: int, session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            select(ClusterAddon.addon_type).where(
                ClusterAddon.cluster_id == cluster_id,
                ClusterAddon.disabled_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def _to_response(
    cluster: TenantCluster,
    *,
    caller_role: str | None,
    session: AsyncSession,
) -> ClusterResponse:
    contract = cluster.contract
    return ClusterResponse(
        id=cluster.id,
        contract_number=contract.contract_number if contract else "",
        name=cluster.name,
        slug=cluster.slug,
        api_url=cluster.api_url,
        worker_groups=cluster.worker_groups,
        initial_worker_groups=cluster.initial_worker_groups,
        size_label=_size_label(cluster.worker_groups),
        total_servers=3 + 3 * cluster.worker_groups,
        provisioned_at=cluster.provisioned_at,
        management_project_resource_name=cluster.management_project_resource_name,
        backup_project_resource_name=cluster.backup_project_resource_name,
        argocd_namespace=cluster.argocd_namespace,
        created_at=cluster.created_at,
        caller_role=caller_role,
        active_addons=await _active_addons(cluster.id, session),
    )


# --- Admin endpoints ---


@admin_router.post("", response_model=ClusterResponse, status_code=201)
async def admin_create_cluster(
    req: CreateClusterRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = (
        await session.execute(
            select(Contract)
            .where(Contract.contract_number == req.contract_number)
            .options(selectinload(Contract.customer))
        )
    ).scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    existing = (
        await session.execute(select(TenantCluster).where(TenantCluster.slug == req.slug))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Cluster slug already in use")

    git_backend: GitBackend = request.app.state.git_backend
    project_name = f"{req.slug}.{contract.customer.domain}"
    try:
        management_resource_name = git_backend.write_project(
            contract_number=contract.contract_number,
            project_name=project_name,
            description=f"SUNET-managed Kubernetes cluster {req.slug}",
            users=[],
            managed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    cluster = TenantCluster(
        contract_id=contract.id,
        name=req.name,
        slug=req.slug,
        api_url=req.api_url,
        ca_bundle=req.ca_bundle,
        openbao_mount=f"kubernetes/{req.slug}",
        openbao_role=req.openbao_role,
        argocd_role_name=req.argocd_role_name,
        argocd_namespace=req.argocd_namespace,
        worker_groups=req.worker_groups,
        initial_worker_groups=req.worker_groups,
        management_project_resource_name=management_resource_name,
        created_by_sub=user["sub"],
    )
    cluster.contract = contract
    session.add(cluster)
    await session.flush()
    await session.commit()

    return await _to_response(cluster, caller_role="sunet_admin", session=session)


@admin_router.get("", response_model=list[ClusterResponse])
async def admin_list_clusters(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(TenantCluster).options(selectinload(TenantCluster.contract))
        )
    ).scalars().all()
    return [await _to_response(c, caller_role="sunet_admin", session=session) for c in rows]


@admin_router.get("/{slug}", response_model=ClusterResponse)
async def admin_get_cluster(
    slug: str,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cluster = (
        await session.execute(
            select(TenantCluster)
            .where(TenantCluster.slug == slug)
            .options(selectinload(TenantCluster.contract))
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return await _to_response(cluster, caller_role="sunet_admin", session=session)


@admin_router.patch("/{slug}", response_model=ClusterResponse)
async def admin_update_cluster(
    slug: str,
    req: UpdateClusterRequest,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cluster = (
        await session.execute(
            select(TenantCluster)
            .where(TenantCluster.slug == slug)
            .options(selectinload(TenantCluster.contract))
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    for field in (
        "name",
        "api_url",
        "ca_bundle",
        "openbao_role",
        "argocd_role_name",
        "argocd_namespace",
    ):
        v = getattr(req, field)
        if v is not None:
            setattr(cluster, field, v)
    await session.commit()
    return await _to_response(cluster, caller_role="sunet_admin", session=session)


@admin_router.post("/{slug}/provision", response_model=ClusterResponse)
async def admin_provision_cluster(
    slug: str,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cluster = (
        await session.execute(
            select(TenantCluster)
            .where(TenantCluster.slug == slug)
            .options(selectinload(TenantCluster.contract))
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if cluster.provisioned_at is not None:
        raise HTTPException(status_code=409, detail="Cluster already provisioned")
    from datetime import datetime, timezone

    cluster.provisioned_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.commit()
    return await _to_response(cluster, caller_role="sunet_admin", session=session)


@admin_router.delete("/{slug}", status_code=204)
async def admin_delete_cluster(
    slug: str,
    request: Request,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cluster = (
        await session.execute(
            select(TenantCluster).where(TenantCluster.slug == slug)
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    git_backend: GitBackend = request.app.state.git_backend
    for resource in (
        cluster.management_project_resource_name,
        cluster.backup_project_resource_name,
    ):
        if not resource:
            continue
        try:
            git_backend.delete_project(resource)
        except ValueError:
            logger.warning("Managed project %s not in git", resource)

    await session.delete(cluster)
    await session.commit()


# --- Member-facing list/get + access mgmt ---


@member_router.get("", response_model=list[ClusterResponse])
async def list_clusters(
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    if is_sunet_admin(user["sub"], settings):
        rows = (
            await session.execute(
                select(TenantCluster).options(selectinload(TenantCluster.contract))
            )
        ).scalars().all()
        return [
            await _to_response(c, caller_role="sunet_admin", session=session)
            for c in rows
        ]

    rows = (
        await session.execute(
            select(TenantCluster, ClusterAccess.role)
            .join(ClusterAccess, ClusterAccess.cluster_id == TenantCluster.id)
            .where(ClusterAccess.user_sub == user["sub"])
            .options(selectinload(TenantCluster.contract))
        )
    ).all()
    return [
        await _to_response(c, caller_role=role, session=session)
        for c, role in rows
    ]


@member_router.get("/{slug}", response_model=ClusterResponse)
async def get_cluster(
    slug: str,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, access = await require_cluster_access(slug, user["sub"], session, settings)
    role = "sunet_admin" if access is None else access.role
    return await _to_response(cluster, caller_role=role, session=session)


@member_router.get("/{slug}/users", response_model=list[ClusterAccessResponse])
async def list_cluster_users(
    slug: str,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, _ = await require_cluster_access(
        slug, user["sub"], session, settings, min_role="customer_admin"
    )
    rows = (
        await session.execute(
            select(ClusterAccess).where(ClusterAccess.cluster_id == cluster.id)
        )
    ).scalars().all()
    return [ClusterAccessResponse.model_validate(r) for r in rows]


@member_router.post(
    "/{slug}/users", response_model=ClusterAccessResponse, status_code=201
)
async def grant_cluster_access(
    slug: str,
    req: ClusterAccessRequest,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, caller_access = await require_cluster_access(
        slug, user["sub"], session, settings, min_role="customer_admin"
    )
    # Only SUNET admins can mint another customer_admin.
    if req.role == "customer_admin" and caller_access is not None:
        raise HTTPException(
            status_code=403,
            detail="Only SUNET admins can grant customer_admin",
        )

    existing = (
        await session.execute(
            select(ClusterAccess).where(
                ClusterAccess.cluster_id == cluster.id,
                ClusterAccess.user_sub == req.user_sub,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User already has access")

    grant = ClusterAccess(
        cluster_id=cluster.id,
        user_sub=req.user_sub,
        role=req.role,
        granted_by_sub=user["sub"],
    )
    session.add(grant)
    await session.commit()
    return ClusterAccessResponse.model_validate(grant)


@member_router.delete("/{slug}/users/{user_sub}", status_code=204)
async def revoke_cluster_access(
    slug: str,
    user_sub: str,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, caller_access = await require_cluster_access(
        slug, user["sub"], session, settings, min_role="customer_admin"
    )
    target = (
        await session.execute(
            select(ClusterAccess).where(
                ClusterAccess.cluster_id == cluster.id,
                ClusterAccess.user_sub == user_sub,
            )
        )
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="No such grant")
    # Customer admin cannot remove a customer_admin (only SUNET can).
    if target.role == "customer_admin" and caller_access is not None:
        raise HTTPException(
            status_code=403,
            detail="Only SUNET admins can remove a customer_admin",
        )

    # Cascade-revoke all of this user's kubeconfigs on this cluster.
    try:
        await kubeconfig_service.cascade_revoke_for_user(
            cluster, user_sub=user_sub, by_sub=user["sub"], session=session
        )
    except Exception:
        # If the cluster is unreachable we still need to revoke portal-side
        # so the user actually loses access; admin can manually clean up
        # orphan RoleBindings later.
        logger.exception(
            "cascade_revoke_for_user failed for cluster %s user %s; "
            "removing access grant anyway",
            slug,
            user_sub,
        )

    await session.delete(target)
    await session.commit()


# Re-exported so main.py can include both with one import.
__all__ = ["admin_router", "member_router"]
