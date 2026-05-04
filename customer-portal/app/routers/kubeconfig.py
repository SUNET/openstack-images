"""User-facing kubeconfig issuance/listing/rotation/revocation.

A kubeconfig issuance is a per-credential X.509 client cert + a per-issuance
RoleBinding in the cluster's argocd namespace. The kubeconfig YAML is
returned exactly once at issue time and never re-fetchable (private key is
not stored). Revocation deletes the RoleBinding; cascade-on-access-removal
is handled in the clusters router.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import kubeconfig_service
from app.audit import audit_log
from app.auth import (
    get_current_user,
    is_sunet_admin,
    require_cluster_access,
)
from app.config import Settings, get_settings
from app.db import get_session
from app.models import KubeconfigIssuance
from app.schemas import (
    IssuedKubeconfigResponse,
    IssueKubeconfigRequest,
    KubeconfigIssuanceResponse,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/clusters", tags=["kubeconfig"])


def _to_metadata(
    issuance: KubeconfigIssuance, cluster_slug: str
) -> KubeconfigIssuanceResponse:
    return KubeconfigIssuanceResponse(
        id=issuance.id,
        cluster_slug=cluster_slug,
        user_sub=issuance.user_sub,
        label=issuance.label,
        cert_serial=issuance.cert_serial,
        expires_at=issuance.expires_at,
        created_at=issuance.created_at,
        revoked_at=issuance.revoked_at,
        revoked_by_sub=issuance.revoked_by_sub,
        status=kubeconfig_service.issuance_status(issuance),
    )


@router.get("/{slug}/credentials", response_model=list[KubeconfigIssuanceResponse])
async def list_credentials(
    slug: str,
    user_sub: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, access = await require_cluster_access(slug, user["sub"], session, settings)
    target = user_sub or user["sub"]
    # Only customer_admin/SUNET admin can list other users' credentials.
    if target != user["sub"]:
        if access is not None and access.role != "customer_admin":
            raise HTTPException(
                status_code=403, detail="Cannot view another user's credentials"
            )
    rows = (
        await session.execute(
            select(KubeconfigIssuance)
            .where(
                KubeconfigIssuance.cluster_id == cluster.id,
                KubeconfigIssuance.user_sub == target,
            )
            .order_by(KubeconfigIssuance.created_at.desc())
        )
    ).scalars().all()
    return [_to_metadata(r, slug) for r in rows]


@router.post(
    "/{slug}/credentials",
    response_model=IssuedKubeconfigResponse,
    status_code=201,
)
async def issue_credential(
    slug: str,
    body: IssueKubeconfigRequest,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, _ = await require_cluster_access(slug, user["sub"], session, settings)
    if cluster.provisioned_at is None:
        raise HTTPException(
            status_code=409,
            detail="Cluster is not yet provisioned",
        )

    ttl_days = kubeconfig_service.default_ttl_days_or(
        body.ttl_days, settings.default_kubeconfig_ttl_days
    )
    issuance, kubeconfig_yaml = await kubeconfig_service.issue(
        cluster,
        user_sub=user["sub"],
        label=body.label,
        ttl_days=ttl_days,
        session=session,
    )
    await session.commit()

    audit_log(
        user["sub"], "kubeconfig.issue",
        cluster=slug, issuance_id=issuance.id,
        cert_serial=issuance.cert_serial, ttl_days=ttl_days,
    )
    meta = _to_metadata(issuance, slug)
    return IssuedKubeconfigResponse(**meta.model_dump(), kubeconfig=kubeconfig_yaml)


@router.post(
    "/{slug}/credentials/{issuance_id}/rotate",
    response_model=IssuedKubeconfigResponse,
)
async def rotate_credential(
    slug: str,
    issuance_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, _ = await require_cluster_access(slug, user["sub"], session, settings)
    old = (
        await session.execute(
            select(KubeconfigIssuance).where(
                KubeconfigIssuance.id == issuance_id,
                KubeconfigIssuance.cluster_id == cluster.id,
            )
        )
    ).scalar_one_or_none()
    if not old:
        raise HTTPException(status_code=404, detail="Issuance not found")
    if old.user_sub != user["sub"]:
        raise HTTPException(status_code=403, detail="Not your issuance")
    if old.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Issuance already revoked")

    ttl_days = settings.default_kubeconfig_ttl_days
    new, kubeconfig_yaml = await kubeconfig_service.issue(
        cluster,
        user_sub=user["sub"],
        label=old.label,
        ttl_days=ttl_days,
        session=session,
    )
    await kubeconfig_service.revoke(
        cluster, old, by_sub=user["sub"], session=session
    )
    await session.commit()
    audit_log(
        user["sub"], "kubeconfig.rotate",
        cluster=slug, old_issuance_id=old.id, new_issuance_id=new.id,
    )
    meta = _to_metadata(new, slug)
    return IssuedKubeconfigResponse(**meta.model_dump(), kubeconfig=kubeconfig_yaml)


@router.delete("/{slug}/credentials/{issuance_id}", status_code=204)
async def revoke_credential(
    slug: str,
    issuance_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    cluster, access = await require_cluster_access(slug, user["sub"], session, settings)
    issuance = (
        await session.execute(
            select(KubeconfigIssuance).where(
                KubeconfigIssuance.id == issuance_id,
                KubeconfigIssuance.cluster_id == cluster.id,
            )
        )
    ).scalar_one_or_none()
    if not issuance:
        raise HTTPException(status_code=404, detail="Issuance not found")

    # Owner, customer_admin on this cluster, or SUNET admin can revoke.
    if issuance.user_sub != user["sub"]:
        if not is_sunet_admin(user["sub"], settings) and (
            access is None or access.role != "customer_admin"
        ):
            raise HTTPException(status_code=403, detail="Cannot revoke another user's credential")

    if issuance.revoked_at is not None:
        return  # already revoked; idempotent

    await kubeconfig_service.revoke(
        cluster, issuance, by_sub=user["sub"], session=session
    )
    await session.commit()
    audit_log(
        user["sub"], "kubeconfig.revoke",
        cluster=slug, issuance_id=issuance.id,
        target_sub=issuance.user_sub,
    )
