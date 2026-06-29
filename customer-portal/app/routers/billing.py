"""Billing job API endpoints."""

import hmac
import json
import logging
from typing import Any

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit_log
from app.auth import get_current_user
from app.billing_runner import (
    execute_job,
    generate_and_deliver,
    get_billing_period,
    run_due_jobs,
)
from app.config import get_settings
from app.crypto import encrypt_value
from app.db import get_session
from app.models import BillingJob, BillingJobContract, BillingJobRun, Contract, ContractAccess
from app.schemas import (
    SENSITIVE_DELIVERY_KEYS,
    BillingJobResponse,
    BillingJobRunResponse,
    CreateBillingJobRequest,
    ManualRunRequest,
    RunOnceRequest,
    RunOnceResponse,
    UpdateBillingJobRequest,
    validate_delivery_config,
)
from app.url_safety import UnsafeDeliveryURL, validate_webdav_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _mask_config(config_json: str) -> dict:
    """Parse delivery config and mask sensitive fields."""
    config = json.loads(config_json)
    for key in list(config):
        if key.lower() in SENSITIVE_DELIVERY_KEYS and config[key]:
            config[key] = "********"
    return config


def _job_to_response(job: BillingJob) -> BillingJobResponse:
    """Build a response from a BillingJob model."""
    contract_ids = [jc.contract_id for jc in (job.selected_contracts or [])]
    return BillingJobResponse(
        id=job.id,
        name=job.name,
        owner_sub=job.owner_sub,
        all_contracts=job.all_contracts,
        contract_ids=contract_ids,
        schedule=job.schedule,
        delivery_method=job.delivery_method,
        delivery_config=_mask_config(job.delivery_config),
        filename_template=job.filename_template,
        per_contract=job.per_contract,
        enabled=job.enabled,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _encrypt_delivery_config(config: dict) -> str:
    """Encrypt sensitive fields in delivery config and return as JSON string."""
    config = dict(config)
    for key in list(config):
        if key.lower() in SENSITIVE_DELIVERY_KEYS and config[key]:
            config[key] = encrypt_value(config[key])
    return json.dumps(config)


async def _validate_contract_access(
    contract_ids: list[int], user_sub: str, is_admin: bool, session: AsyncSession
) -> None:
    """Validate that the user has access to all specified contracts."""
    if is_admin:
        # Admin can access any contract, just verify they exist
        for cid in contract_ids:
            c = await session.get(Contract, cid)
            if not c:
                raise HTTPException(status_code=404, detail=f"Contract {cid} not found")
    else:
        for cid in contract_ids:
            result = await session.execute(
                select(ContractAccess).where(
                    ContractAccess.contract_id == cid,
                    ContractAccess.user_sub == user_sub,
                )
            )
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=403, detail=f"No access to contract {cid}")


def _validate_schedule(schedule: str) -> None:
    """Validate a cron expression."""
    try:
        croniter(schedule)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron schedule: {e}")


def _validate_delivery_config(method: str, config: dict) -> dict:
    """Run the typed schema for `method` and apply method-specific safety checks.

    For WebDAV: enforces https + WEBDAV_ALLOWED_HOSTS allowlist + reject any
    private/loopback/link-local resolution. For email: schema validates the
    recipient pattern. Returns the normalized config dict.
    """
    settings = get_settings()
    try:
        normalized = validate_delivery_config(method, config)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=e.errors()) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if method == "webdav":
        try:
            validate_webdav_url(normalized["url"], settings.webdav_allowed_hosts)
        except UnsafeDeliveryURL as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return normalized


async def _require_job_access(
    job_id: int, user: dict, session: AsyncSession
) -> BillingJob:
    """Load a job and verify the user has access."""
    settings = get_settings()
    result = await session.execute(
        select(BillingJob)
        .where(BillingJob.id == job_id)
        .options(selectinload(BillingJob.selected_contracts))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Billing job not found")
    if job.owner_sub != user["sub"] and user["sub"] not in settings.admin_users:
        raise HTTPException(status_code=403, detail="Not authorized")
    return job


# --- Endpoints ---


@router.get("/jobs", response_model=list[BillingJobResponse])
async def list_jobs(
    all: bool = False,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    is_admin = user["sub"] in settings.admin_users

    if all and is_admin:
        result = await session.execute(
            select(BillingJob)
            .options(selectinload(BillingJob.selected_contracts))
            .order_by(BillingJob.name)
        )
    else:
        result = await session.execute(
            select(BillingJob)
            .where(BillingJob.owner_sub == user["sub"])
            .options(selectinload(BillingJob.selected_contracts))
            .order_by(BillingJob.name)
        )
    return [_job_to_response(j) for j in result.scalars()]


@router.post("/jobs", response_model=BillingJobResponse, status_code=201)
async def create_job(
    req: CreateBillingJobRequest,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    is_admin = user["sub"] in settings.admin_users

    _validate_schedule(req.schedule)
    normalized_config = _validate_delivery_config(req.delivery_method, req.delivery_config)

    if not req.all_contracts:
        await _validate_contract_access(req.contract_ids, user["sub"], is_admin, session)

    job = BillingJob(
        name=req.name,
        owner_sub=user["sub"],
        all_contracts=req.all_contracts,
        schedule=req.schedule,
        delivery_method=req.delivery_method,
        delivery_config=_encrypt_delivery_config(normalized_config),
        filename_template=req.filename_template,
        per_contract=req.per_contract,
        enabled=req.enabled,
    )
    session.add(job)
    await session.flush()

    if not req.all_contracts:
        for cid in req.contract_ids:
            session.add(BillingJobContract(billing_job_id=job.id, contract_id=cid))

    await session.commit()
    await session.refresh(job, ["selected_contracts"])
    audit_log(
        user["sub"],
        "billing_job.create",
        job_id=job.id,
        delivery_method=job.delivery_method,
    )
    return _job_to_response(job)


@router.get("/jobs/{job_id}", response_model=BillingJobResponse)
async def get_job(
    job_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    job = await _require_job_access(job_id, user, session)
    return _job_to_response(job)


@router.patch("/jobs/{job_id}", response_model=BillingJobResponse)
async def update_job(
    job_id: int,
    req: UpdateBillingJobRequest,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    is_admin = user["sub"] in settings.admin_users
    job = await _require_job_access(job_id, user, session)

    if req.schedule is not None:
        _validate_schedule(req.schedule)
        job.schedule = req.schedule

    if req.delivery_method is not None:
        job.delivery_method = req.delivery_method

    if req.delivery_config is not None:
        method = req.delivery_method or job.delivery_method
        # If any sensitive field is masked, splice in the existing encrypted
        # value before validation so we don't re-encrypt the placeholder.
        old_config = json.loads(job.delivery_config)
        new_config = dict(req.delivery_config)
        masked_fields: list[str] = []
        for key in list(new_config):
            if (
                key.lower() in SENSITIVE_DELIVERY_KEYS
                and new_config[key] == "********"
            ):
                new_config[key] = ""  # placeholder, stripped below
                masked_fields.append(key)
        normalized = _validate_delivery_config(method, new_config)
        # Restore the masked-out encrypted values from the existing record
        # *after* schema validation passed.
        for key in masked_fields:
            normalized[key] = old_config.get(key, "")
        # Encrypt only the new (plaintext) sensitive values; for masked
        # fields, normalized[key] is already the stored ciphertext.
        encrypted = dict(normalized)
        for key in list(encrypted):
            if (
                key.lower() in SENSITIVE_DELIVERY_KEYS
                and encrypted[key]
                and key not in masked_fields
            ):
                encrypted[key] = encrypt_value(encrypted[key])
        job.delivery_config = json.dumps(encrypted)

    for field in ("name", "all_contracts", "filename_template", "per_contract", "enabled"):
        val = getattr(req, field)
        if val is not None:
            setattr(job, field, val)

    if req.contract_ids is not None:
        if not (req.all_contracts if req.all_contracts is not None else job.all_contracts):
            await _validate_contract_access(req.contract_ids, user["sub"], is_admin, session)
            # Replace junction entries
            for jc in list(job.selected_contracts):
                await session.delete(jc)
            await session.flush()
            for cid in req.contract_ids:
                session.add(BillingJobContract(billing_job_id=job.id, contract_id=cid))

    await session.commit()
    await session.refresh(job, ["selected_contracts"])
    audit_log(user["sub"], "billing_job.update", job_id=job.id)
    return _job_to_response(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    job = await _require_job_access(job_id, user, session)
    await session.delete(job)
    await session.commit()
    audit_log(user["sub"], "billing_job.delete", job_id=job_id)


@router.get("/jobs/{job_id}/runs", response_model=list[BillingJobRunResponse])
async def list_runs(
    job_id: int,
    limit: int = 20,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _require_job_access(job_id, user, session)
    result = await session.execute(
        select(BillingJobRun)
        .where(BillingJobRun.billing_job_id == job_id)
        .order_by(BillingJobRun.started_at.desc())
        .limit(min(limit, 100))
    )
    return result.scalars().all()


@router.post("/jobs/{job_id}/run", response_model=BillingJobRunResponse)
async def manual_run(
    job_id: int,
    req: ManualRunRequest = ManualRunRequest(),
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    job = await _require_job_access(job_id, user, session)
    run = await execute_job(session, job, year=req.year, month=req.month)
    return run


# --- Ad-hoc run-once (no saved job) ---


async def _resolve_run_once_contracts(
    req: RunOnceRequest, user_sub: str, is_admin: bool, session: AsyncSession
) -> list[str]:
    """Resolve contract numbers for an ad-hoc run, enforcing access."""
    if req.all_contracts:
        if is_admin:
            result = await session.execute(select(Contract.contract_number))
        else:
            result = await session.execute(
                select(Contract.contract_number)
                .join(ContractAccess)
                .where(ContractAccess.user_sub == user_sub)
            )
        return [r[0] for r in result]

    await _validate_contract_access(req.contract_ids, user_sub, is_admin, session)
    result = await session.execute(
        select(Contract.contract_number).where(Contract.id.in_(req.contract_ids))
    )
    return [r[0] for r in result]


@router.post("/run-once", response_model=RunOnceResponse)
async def run_once(
    req: RunOnceRequest,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate and deliver a billing export once, without saving a job."""
    settings = get_settings()
    is_admin = user["sub"] in settings.admin_users

    normalized_config = _validate_delivery_config(req.delivery_method, req.delivery_config)
    contract_numbers = await _resolve_run_once_contracts(
        req, user["sub"], is_admin, session
    )

    period_start, period_end = get_billing_period(req.year, req.month)

    if not contract_numbers:
        audit_log(
            user["sub"], "billing.run_once",
            files=0, period=period_start.strftime("%Y-%m"), status="empty",
        )
        return RunOnceResponse(
            status="success",
            files_delivered=0,
            billing_period_start=period_start,
            billing_period_end=period_end,
        )

    try:
        files = await generate_and_deliver(
            settings,
            contract_numbers,
            req.delivery_method,
            normalized_config,
            req.filename_template,
            req.per_contract,
            period_start,
            period_end,
        )
    except Exception as e:
        logger.exception("Ad-hoc billing run failed for %s", user["sub"])
        audit_log(
            user["sub"], "billing.run_once",
            period=period_start.strftime("%Y-%m"), status="error",
        )
        return RunOnceResponse(
            status="error",
            files_delivered=0,
            billing_period_start=period_start,
            billing_period_end=period_end,
            error_message=str(e)[:500],
        )

    audit_log(
        user["sub"], "billing.run_once",
        files=files, period=period_start.strftime("%Y-%m"), status="success",
    )
    return RunOnceResponse(
        status="success",
        files_delivered=files,
        billing_period_start=period_start,
        billing_period_end=period_end,
    )


# --- Trigger endpoint (called by CronJob) ---


@router.post("/run-due")
async def trigger_run_due(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    if not settings.billing_trigger_token:
        raise HTTPException(status_code=503, detail="Billing trigger not configured")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = auth[7:]
    if not hmac.compare_digest(token, settings.billing_trigger_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    runs = await run_due_jobs(session)
    return {
        "triggered": len(runs),
        "results": [
            {"job_id": r.billing_job_id, "status": r.status}
            for r in runs
        ],
    }
