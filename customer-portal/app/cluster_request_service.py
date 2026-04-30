"""Customer-admin change requests (addons, resize, backup) and SUNET-admin apply.

Each request lives as a row in `cluster_request` until a SUNET admin applies
or denies it. Application is dispatched by request type:

  addon enable/disable  -> insert/stamp ClusterAddon row
  resize                -> bump TenantCluster.worker_groups, record `before` in payload
  backup enable         -> write managed OpenstackProject CR, link from cluster
  backup disable        -> delete the CR, unlink

The actual installation work (kubespray runs, ArgoCD app pushes) happens
out-of-band on receipt of the email notification; this service only mutates
portal state and sends the email.
"""

import asyncio
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.git_backend import GitBackend
from app.models import ClusterAddon, ClusterRequest, Contract, TenantCluster
from app.schemas import (
    AddonRequestPayload,
    BackupRequestPayload,
    ResizeRequestPayload,
)

logger = logging.getLogger(__name__)


_VALID_TYPES = {"addon", "resize", "backup"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_payload(request_type: str, payload: dict) -> dict:
    """Returns the validated payload as a JSON-serialisable dict."""
    if request_type == "addon":
        return AddonRequestPayload(**payload).model_dump()
    if request_type == "resize":
        return ResizeRequestPayload(**payload).model_dump()
    if request_type == "backup":
        return BackupRequestPayload(**payload).model_dump()
    raise HTTPException(status_code=400, detail=f"Unknown request_type: {request_type}")


def _send_ops_email(cluster: TenantCluster, request: ClusterRequest) -> None:
    """Best-effort email to SUNET ops; failures are logged, never raised."""
    settings = get_settings()
    if not settings.smtp_host or not settings.sunet_ops_email:
        logger.info("SMTP or SUNET_OPS_EMAIL not configured; skipping ops email")
        return

    msg = EmailMessage()
    msg["Subject"] = (
        f"[customer-portal] {request.request_type} request for cluster {cluster.slug}"
    )
    msg["From"] = settings.smtp_from
    msg["To"] = settings.sunet_ops_email
    body = (
        f"Cluster: {cluster.slug} (id {cluster.id})\n"
        f"Request type: {request.request_type}\n"
        f"Requested by: {request.requested_by_sub}\n"
        f"Payload: {request.payload}\n\n"
        f"Review and apply at: {settings.base_url or ''}#/admin/cluster-requests\n"
    )
    msg.set_content(body)

    def _send() -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            if settings.smtp_username:
                smtp.starttls()
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)

    try:
        # Run inline; this code is already inside an async handler — but the
        # call site awaits via asyncio.to_thread so a slow SMTP doesn't block.
        _send()
    except Exception as exc:  # noqa: BLE001 — email is best-effort
        logger.warning("Failed to send ops email for request %s: %s", request.id, exc)


async def create_request(
    cluster: TenantCluster,
    *,
    requested_by: str,
    request_type: str,
    payload: dict,
    session: AsyncSession,
) -> ClusterRequest:
    if request_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown request_type: {request_type}")

    validated = _validate_payload(request_type, payload)

    # Cheap pre-checks so the customer admin gets fast feedback rather than
    # the SUNET admin having to deny obvious mistakes.
    if request_type == "resize":
        target = validated["target_worker_groups"]
        if target <= cluster.worker_groups:
            raise HTTPException(
                status_code=400,
                detail=f"target_worker_groups must be > current ({cluster.worker_groups})",
            )
    elif request_type == "backup":
        if validated["action"] == "enable" and cluster.backup_project_resource_name:
            raise HTTPException(status_code=409, detail="Backup is already enabled")
        if validated["action"] == "disable" and not cluster.backup_project_resource_name:
            raise HTTPException(status_code=409, detail="Backup is not enabled")

    request = ClusterRequest(
        cluster_id=cluster.id,
        request_type=request_type,
        payload=json.dumps(validated),
        status="pending",
        requested_by_sub=requested_by,
    )
    session.add(request)
    await session.flush()

    # Email asynchronously so SMTP latency doesn't block the response.
    await asyncio.to_thread(_send_ops_email, cluster, request)
    return request


async def _apply_addon(
    cluster: TenantCluster, payload: dict, by_sub: str, session: AsyncSession
) -> None:
    addon_type = payload["addon_type"]
    if payload["action"] == "enable":
        existing = (
            await session.execute(
                select(ClusterAddon).where(
                    ClusterAddon.cluster_id == cluster.id,
                    ClusterAddon.addon_type == addon_type,
                    ClusterAddon.disabled_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail="Addon already enabled")
        session.add(
            ClusterAddon(
                cluster_id=cluster.id,
                addon_type=addon_type,
                enabled_by_sub=by_sub,
            )
        )
    else:
        existing = (
            await session.execute(
                select(ClusterAddon).where(
                    ClusterAddon.cluster_id == cluster.id,
                    ClusterAddon.addon_type == addon_type,
                    ClusterAddon.disabled_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not existing:
            raise HTTPException(status_code=409, detail="Addon is not enabled")
        existing.disabled_at = _now()
        existing.disabled_by_sub = by_sub


async def _apply_resize(
    cluster: TenantCluster, request: ClusterRequest, payload: dict, session: AsyncSession
) -> None:
    target = payload["target_worker_groups"]
    if target <= cluster.worker_groups:
        raise HTTPException(
            status_code=400,
            detail=f"target_worker_groups must be > current ({cluster.worker_groups})",
        )
    # Stamp the before-count into the payload so the billing engine can
    # compute the expansion-fee delta from this row alone.
    payload["before_worker_groups"] = cluster.worker_groups
    request.payload = json.dumps(payload)
    cluster.worker_groups = target


async def _apply_backup(
    cluster: TenantCluster,
    payload: dict,
    by_sub: str,
    session: AsyncSession,
    git_backend: GitBackend,
) -> None:
    contract = (
        await session.execute(
            select(Contract)
            .where(Contract.id == cluster.contract_id)
            .options(selectinload(Contract.customer))
        )
    ).scalar_one()

    if payload["action"] == "enable":
        if cluster.backup_project_resource_name:
            raise HTTPException(status_code=409, detail="Backup is already enabled")
        project_name = f"{cluster.slug}-backup.{contract.customer.domain}"
        resource_name = git_backend.write_project(
            contract_number=contract.contract_number,
            project_name=project_name,
            description=f"Backup storage for tenant cluster {cluster.slug}",
            users=[],
            managed=True,
        )
        cluster.backup_project_resource_name = resource_name
    else:
        if not cluster.backup_project_resource_name:
            raise HTTPException(status_code=409, detail="Backup is not enabled")
        try:
            git_backend.delete_project(cluster.backup_project_resource_name)
        except ValueError:
            logger.warning(
                "Backup project %s not in git; clearing link",
                cluster.backup_project_resource_name,
            )
        cluster.backup_project_resource_name = None


async def apply_request(
    request: ClusterRequest,
    *,
    by_sub: str,
    note: str | None,
    session: AsyncSession,
    git_backend: GitBackend,
) -> None:
    if request.status != "pending":
        raise HTTPException(status_code=409, detail=f"Request already {request.status}")

    cluster = (
        await session.execute(
            select(TenantCluster).where(TenantCluster.id == request.cluster_id)
        )
    ).scalar_one()

    payload = json.loads(request.payload)

    if request.request_type == "addon":
        await _apply_addon(cluster, payload, by_sub, session)
    elif request.request_type == "resize":
        await _apply_resize(cluster, request, payload, session)
    elif request.request_type == "backup":
        await _apply_backup(cluster, payload, by_sub, session, git_backend)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown request_type: {request.request_type}",
        )

    request.status = "applied"
    request.applied_by_sub = by_sub
    request.applied_at = _now()
    request.note = note
    await session.flush()


async def deny_request(
    request: ClusterRequest,
    *,
    by_sub: str,
    note: str | None,
    session: AsyncSession,
) -> None:
    if request.status != "pending":
        raise HTTPException(status_code=409, detail=f"Request already {request.status}")
    request.status = "denied"
    request.applied_by_sub = by_sub
    request.applied_at = _now()
    request.note = note
    await session.flush()
