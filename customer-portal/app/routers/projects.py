"""Customer endpoints for managing projects under contracts."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user, is_sunet_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.git_backend import GitBackend
from app.k8s import get_project_status
from app.models import Contract, ContractAccess
from app.schemas import CreateProjectRequest, ProjectResponse, UpdateProjectRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contracts", tags=["projects"])


async def _require_contract_access(
    contract_number: str, user_sub: str, session: AsyncSession
) -> Contract:
    """Verify user has access to the contract. Returns the contract with customer loaded."""
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
        phase=status.get("phase") if status else "Pending (not synced)",
        managed=proj.get("managed", False),
    )


@router.get("/{contract_number}/projects", response_model=list[ProjectResponse])
async def list_projects(
    contract_number: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session)

    git_backend: GitBackend = request.app.state.git_backend
    projects = git_backend.list_projects(contract_number)
    return [_enrich_project(p) for p in projects]


@router.get("/{contract_number}/projects/{resource_name}", response_model=ProjectResponse)
async def get_project(
    contract_number: str,
    resource_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _require_contract_access(contract_number, user["sub"], session)

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
    session: AsyncSession = Depends(get_session),
):
    contract = await _require_contract_access(contract_number, user["sub"], session)

    # Qualify project name with customer domain
    qualified_name = f"{req.name}.{contract.customer.domain}"

    # Ensure the creating user is in the users list
    users = list(set(req.users + [user["sub"]]))

    git_backend: GitBackend = request.app.state.git_backend
    try:
        resource_name = git_backend.write_project(
            contract_number=contract_number,
            project_name=qualified_name,
            description=req.description,
            users=users,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return ProjectResponse(
        resource_name=resource_name,
        name=qualified_name,
        description=req.description,
        contract_number=contract_number,
        users=users,
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
    await _require_contract_access(contract_number, user["sub"], session)

    git_backend: GitBackend = request.app.state.git_backend

    existing = git_backend.get_project(resource_name)
    if not existing or existing["contract_number"] != contract_number:
        raise HTTPException(status_code=404, detail="Project not found")

    if existing.get("managed") and not is_sunet_admin(user["sub"], settings):
        raise HTTPException(
            status_code=403, detail="This project is SUNET-managed and read-only"
        )

    try:
        updated = git_backend.update_project(
            resource_name=resource_name,
            description=req.description,
            users=req.users,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

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
    await _require_contract_access(contract_number, user["sub"], session)

    git_backend: GitBackend = request.app.state.git_backend

    existing = git_backend.get_project(resource_name)
    if not existing or existing["contract_number"] != contract_number:
        raise HTTPException(status_code=404, detail="Project not found")

    if existing.get("managed") and not is_sunet_admin(user["sub"], settings):
        raise HTTPException(
            status_code=403, detail="This project is SUNET-managed and read-only"
        )

    try:
        git_backend.delete_project(resource_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
