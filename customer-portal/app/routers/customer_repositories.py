"""SUNET-admin-only configuration of shared customer/environment repositories."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit_log
from app.auth import require_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.models import CustomerClusterRepository
from app.repository_schemas import (
    CredentialKind,
    RepositoryCredentialsRequest,
    RepositoryResponse,
    RepositoryUpdateRequest,
    RepositoryValidateRequest,
)
from app.repository_service import (
    CredentialReplacement,
    get_repository,
    replace_credentials,
    repository_response,
    save_repository,
    validate_repository,
)

router = APIRouter(
    prefix="/api/admin/customers/{customer_id}/cluster-repository",
    tags=["admin-customer-repositories"],
)


@router.get("", response_model=RepositoryResponse)
async def read_repository(
    customer_id: int,
    _user: dict[str, Any] = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RepositoryResponse:
    repository = await get_repository(customer_id, settings, session)
    return await repository_response(repository, customer_id, settings, session)


async def _saved_response(
    repository: CustomerClusterRepository,
    user: dict[str, Any],
    action: str,
    settings: Settings,
    session: AsyncSession,
    *,
    credential_write: CredentialReplacement | None = None,
) -> RepositoryResponse:
    # Build the view in the same locked transaction, avoiding a mixed-version response.
    try:
        response = await repository_response(repository, repository.customer_id, settings, session)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        if credential_write is not None:
            raise HTTPException(503, credential_write.commit_failure().model_dump()) from None
        raise HTTPException(
            503, "Repository configuration could not be committed; reload it"
        ) from None
    audit_log(
        user["sub"], action,
        customer_id=response.customer_id,
        repository_id=response.id,
        environment=response.environment,
        version=response.version,
    )
    return response


@router.put("", response_model=RepositoryResponse)
async def update_repository(
    customer_id: int,
    req: RepositoryUpdateRequest,
    user: dict[str, Any] = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RepositoryResponse:
    repository = await save_repository(customer_id, req, settings, session)
    return await _saved_response(repository, user, "repository.configure", settings, session)


@router.post("/credentials/{kind}", response_model=RepositoryResponse)
async def update_credentials(
    customer_id: int,
    kind: CredentialKind,
    req: RepositoryCredentialsRequest,
    user: dict[str, Any] = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RepositoryResponse:
    """Replace credentials with CAS, optionally confirming a recovery version.

    Reader replacements reject the configured writer's token. Token scope and
    separation after writer replacement require manual verification in Forgejo.
    """
    replacement = await replace_credentials(customer_id, kind, req, settings, session)
    return await _saved_response(
        replacement.repository, user, f"repository.rotate_{kind}", settings, session,
        credential_write=replacement,
    )


@router.post("/validate", response_model=RepositoryResponse)
async def check_repository(
    customer_id: int,
    req: RepositoryValidateRequest,
    user: dict[str, Any] = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RepositoryResponse:
    repository = await validate_repository(customer_id, req.expected_version, settings, session)
    return await _saved_response(repository, user, "repository.validate", settings, session)
