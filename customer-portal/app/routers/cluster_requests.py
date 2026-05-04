"""Cluster change-request workflow.

Customer admins POST requests; SUNET admins apply or deny them from a global
queue. Requests doubly serve as the audit trail for resize-driven setup
fees, so the billing engine reads applied resize requests directly.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cluster_request_service
from app.audit import audit_log
from app.auth import get_current_user, require_admin, require_cluster_access
from app.config import Settings, get_settings
from app.db import get_session
from app.models import ClusterRequest, TenantCluster
from app.schemas import (
    ApplyOrDenyRequestRequest,
    ClusterRequestResponse,
    CreateClusterRequestRequest,
)

logger = logging.getLogger(__name__)


member_router = APIRouter(prefix="/api/clusters", tags=["cluster-requests"])
admin_router = APIRouter(prefix="/api/admin/cluster-requests", tags=["admin-cluster-requests"])


def _to_response(req: ClusterRequest, cluster_slug: str) -> ClusterRequestResponse:
    return ClusterRequestResponse(
        id=req.id,
        cluster_id=req.cluster_id,
        cluster_slug=cluster_slug,
        request_type=req.request_type,
        payload=json.loads(req.payload),
        status=req.status,
        requested_by_sub=req.requested_by_sub,
        requested_at=req.requested_at,
        applied_by_sub=req.applied_by_sub,
        applied_at=req.applied_at,
        note=req.note,
    )


# --- Member-facing ---


@member_router.get(
    "/{slug}/requests", response_model=list[ClusterRequestResponse]
)
async def list_requests(
    slug: str,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, access = await require_cluster_access(slug, user["sub"], session, settings)
    stmt = select(ClusterRequest).where(ClusterRequest.cluster_id == cluster.id)
    # Regular users can see only their own requests.
    if access is not None and access.role == "user":
        stmt = stmt.where(ClusterRequest.requested_by_sub == user["sub"])
    stmt = stmt.order_by(ClusterRequest.requested_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_response(r, slug) for r in rows]


@member_router.post(
    "/{slug}/requests",
    response_model=ClusterRequestResponse,
    status_code=201,
)
async def create_request(
    slug: str,
    body: CreateClusterRequestRequest,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, _ = await require_cluster_access(
        slug, user["sub"], session, settings, min_role="customer_admin"
    )
    req = await cluster_request_service.create_request(
        cluster,
        requested_by=user["sub"],
        request_type=body.request_type,
        payload=body.payload,
        session=session,
    )
    await session.commit()
    audit_log(
        user["sub"], "cluster_request.create",
        cluster=slug, request_id=req.id, request_type=req.request_type,
    )
    return _to_response(req, slug)


# --- Admin-facing ---


@admin_router.get("", response_model=list[ClusterRequestResponse])
async def admin_list_requests(
    status: str | None = None,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(ClusterRequest, TenantCluster.slug)
        .join(TenantCluster, TenantCluster.id == ClusterRequest.cluster_id)
        .order_by(ClusterRequest.requested_at.desc())
    )
    if status:
        stmt = stmt.where(ClusterRequest.status == status)
    rows = (await session.execute(stmt)).all()
    return [_to_response(r, slug) for r, slug in rows]


async def _load_request_for_admin(
    request_id: int, session: AsyncSession
) -> tuple[ClusterRequest, str]:
    row = (
        await session.execute(
            select(ClusterRequest, TenantCluster.slug)
            .join(TenantCluster, TenantCluster.id == ClusterRequest.cluster_id)
            .where(ClusterRequest.id == request_id)
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return row[0], row[1]


@admin_router.post(
    "/{request_id}/apply", response_model=ClusterRequestResponse
)
async def admin_apply_request(
    request_id: int,
    body: ApplyOrDenyRequestRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cr, slug = await _load_request_for_admin(request_id, session)
    await cluster_request_service.apply_request(
        cr,
        by_sub=user["sub"],
        note=body.note,
        session=session,
        git_backend=request.app.state.git_backend,
    )
    await session.commit()
    audit_log(
        user["sub"], "cluster_request.apply",
        cluster=slug, request_id=request_id, request_type=cr.request_type,
    )
    return _to_response(cr, slug)


@admin_router.post(
    "/{request_id}/deny", response_model=ClusterRequestResponse
)
async def admin_deny_request(
    request_id: int,
    body: ApplyOrDenyRequestRequest,
    user: dict[str, Any] = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cr, slug = await _load_request_for_admin(request_id, session)
    await cluster_request_service.deny_request(
        cr, by_sub=user["sub"], note=body.note, session=session
    )
    await session.commit()
    audit_log(
        user["sub"], "cluster_request.deny",
        cluster=slug, request_id=request_id, request_type=cr.request_type,
    )
    return _to_response(cr, slug)


__all__ = ["admin_router", "member_router"]
