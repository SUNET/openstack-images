"""Customer endpoints for managing projects under contracts."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit_log
from app.auth import get_current_user, is_sunet_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.git_backend import (
    GitBackend,
    ManagedProjectMutationError,
    require_contract_renamable,
    require_project_mutable,
)
from app.k8s import find_project_cr_by_spec_name, get_project_status
from app.models import Contract, ContractAccess
from app.schemas import CreateProjectRequest, ProjectResponse, UpdateProjectRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contracts", tags=["projects"])


def _require_project_mutable(project: dict) -> None:
    """Reject generic mutations of cluster-managed project state."""
    try:
        require_project_mutable(project)
    except ManagedProjectMutationError as e:
        raise HTTPException(status_code=403, detail=str(e))


def _require_contract_renamable(projects: list[dict]) -> None:
    """Reject renaming a contract that contains managed project state."""
    try:
        require_contract_renamable(projects)
    except ManagedProjectMutationError as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _require_contract_access(
    contract_number: str,
    user_sub: str,
    session: AsyncSession,
    settings: Settings | None = None,
) -> Contract:
    """Verify user has access to the contract. Returns the contract with customer loaded.

    SUNET admins bypass the ContractAccess check for support and visibility.
    Managed-project mutation is rejected separately for every caller.
    """
    if settings is not None and is_sunet_admin(user_sub, settings):
        result = await session.execute(
            select(Contract)
            .where(Contract.contract_number == contract_number)
            .options(selectinload(Contract.customer))
        )
        contract = result.scalar_one_or_none()
        if not contract:
            raise HTTPException(status_code=404, detail="Contract not found")
        return contract

    result = await session.execute(
        select(Contract)
        .join(ContractAccess)
        .where(
            Contract.contract_number == contract_number,
            ContractAccess.user_sub == user_sub,
        )
        .options(selectinload(Contract.customer))
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=403, detail="No access to this contract")
    return contract


def _enrich_project(proj: dict) -> ProjectResponse:
    """Add K8s status to a project dict."""
    status = get_project_status(proj["resource_name"])
    return ProjectResponse(
        resource_name=proj["resource_name"],
        name=proj["name"],
        description=proj["description"],
        contract_number=proj["contract_number"],
        users=proj["users"],
        quotas=proj.get("quotas"),
        phase=status.get("phase") if status else "Pending (not synced)",
        managed=proj.get("managed", False),
    )


@router.get("/{contract_number}/projects", response_model=list[ProjectResponse])
async def list_projects(
    contract_number: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session, settings)

    git_backend: GitBackend = request.app.state.git_backend
    projects = git_backend.list_projects(contract_number)
    return [_enrich_project(p) for p in projects]


@router.get("/{contract_number}/projects/{resource_name}", response_model=ProjectResponse)
async def get_project(
    contract_number: str,
    resource_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session, settings)

    git_backend: GitBackend = request.app.state.git_backend
    proj = git_backend.get_project(resource_name)
    if not proj or proj["contract_number"] != contract_number:
        raise HTTPException(status_code=404, detail="Project not found")

    return _enrich_project(proj)


@router.post("/{contract_number}/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    contract_number: str,
    req: CreateProjectRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    contract = await _require_contract_access(contract_number, user["sub"], session, settings)

    # Qualify project name with customer domain
    qualified_name = f"{req.name}.{contract.customer.domain}"

    # Ensure the creating user is in the users list
    users = list(set(req.users + [user["sub"]]))

    # Reject names already taken by a CR outside the portal's git repo
    # (e.g. applied directly through ArgoCD) — a duplicate would fight
    # over the same OpenStack project.
    existing_cr = find_project_cr_by_spec_name(qualified_name)
    if existing_cr:
        raise HTTPException(
            status_code=409,
            detail=f"A project named '{qualified_name}' already exists",
        )

    git_backend: GitBackend = request.app.state.git_backend
    try:
        resource_name = git_backend.write_project(
            contract_number=contract_number,
            project_name=qualified_name,
            description=req.description,
            users=users,
            quotas=req.quotas.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    audit_log(
        user["sub"],
        "project.create",
        contract_number=contract_number,
        resource_name=resource_name,
        users=len(users),
    )
    return ProjectResponse(
        resource_name=resource_name,
        name=qualified_name,
        description=req.description,
        contract_number=contract_number,
        users=users,
        quotas=req.quotas,
        phase="Pending (waiting for ArgoCD sync)",
    )


@router.patch("/{contract_number}/projects/{resource_name}", response_model=ProjectResponse)
async def update_project(
    contract_number: str,
    resource_name: str,
    req: UpdateProjectRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session, settings)

    git_backend: GitBackend = request.app.state.git_backend

    existing = git_backend.get_project(resource_name)
    if not existing or existing["contract_number"] != contract_number:
        raise HTTPException(status_code=404, detail="Project not found")

    _require_project_mutable(existing)

    try:
        updated = git_backend.update_project(
            resource_name=resource_name,
            description=req.description,
            users=req.users,
            quotas=req.quotas.model_dump() if req.quotas is not None else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    audit_log(
        user["sub"],
        "project.update",
        contract_number=contract_number,
        resource_name=resource_name,
        users=len(req.users) if req.users is not None else "unchanged",
        quotas="changed" if req.quotas is not None else "unchanged",
    )
    return _enrich_project(updated)


@router.delete("/{contract_number}/projects/{resource_name}", status_code=204)
async def delete_project(
    contract_number: str,
    resource_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session, settings)

    git_backend: GitBackend = request.app.state.git_backend

    existing = git_backend.get_project(resource_name)
    if not existing or existing["contract_number"] != contract_number:
        raise HTTPException(status_code=404, detail="Project not found")

    _require_project_mutable(existing)

    try:
        git_backend.delete_project(resource_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    audit_log(
        user["sub"],
        "project.delete",
        contract_number=contract_number,
        resource_name=resource_name,
    )
