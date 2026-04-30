"""OIDC authentication and session management."""

import logging
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.models import ClusterAccess, Contract, ContractAccess, TenantCluster

logger = logging.getLogger(__name__)

oauth = OAuth()


def init_oauth(settings: Settings) -> None:
    """Register the OIDC provider with authlib."""
    oauth.register(
        name="oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )


def get_current_user(request: Request) -> dict[str, Any]:
    """Extract the current user from the session. Raises 401 if not logged in."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Require the current user to be an admin."""
    user = get_current_user(request)
    if user["sub"] not in settings.admin_users:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def get_user_contracts(
    user_sub: str, session: AsyncSession
) -> list[Contract]:
    """Get all contracts a user has access to."""
    result = await session.execute(
        select(Contract)
        .join(ContractAccess)
        .where(ContractAccess.user_sub == user_sub)
        .options(selectinload(Contract.customer))
    )
    return list(result.scalars().all())


# --- Cluster access ---


def is_sunet_admin(user_sub: str, settings: Settings) -> bool:
    return user_sub in settings.admin_users


async def get_user_clusters(
    user_sub: str,
    session: AsyncSession,
    settings: Settings,
) -> list[TenantCluster]:
    """Clusters the user can see. SUNET admin sees everything."""
    if is_sunet_admin(user_sub, settings):
        result = await session.execute(
            select(TenantCluster).options(selectinload(TenantCluster.contract))
        )
    else:
        result = await session.execute(
            select(TenantCluster)
            .join(ClusterAccess)
            .where(ClusterAccess.user_sub == user_sub)
            .options(selectinload(TenantCluster.contract))
        )
    return list(result.scalars().all())


async def require_cluster_access(
    cluster_slug: str,
    user_sub: str,
    session: AsyncSession,
    settings: Settings,
    *,
    min_role: str = "user",
) -> tuple[TenantCluster, ClusterAccess | None]:
    """Verify the user can act on this cluster at the requested role tier.

    Returns the cluster and the user's ClusterAccess row. SUNET admins return
    `(cluster, None)` and bypass cluster-level role checks.

    Raises 404 if the cluster does not exist; 403 if access is insufficient.
    """
    cluster = (
        await session.execute(
            select(TenantCluster)
            .where(TenantCluster.slug == cluster_slug)
            .options(selectinload(TenantCluster.contract))
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    if is_sunet_admin(user_sub, settings):
        return cluster, None

    access = (
        await session.execute(
            select(ClusterAccess).where(
                ClusterAccess.cluster_id == cluster.id,
                ClusterAccess.user_sub == user_sub,
            )
        )
    ).scalar_one_or_none()
    if not access:
        raise HTTPException(status_code=403, detail="No access to this cluster")

    if min_role == "customer_admin" and access.role != "customer_admin":
        raise HTTPException(
            status_code=403, detail="Customer admin role required"
        )

    return cluster, access
