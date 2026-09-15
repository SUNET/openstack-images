"""Admin-only cluster GitOps lifecycle; no implicit publication from reads or edits."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import gitops_service as service
from app.audit import audit_log
from app.auth import require_admin
from app.config import Settings, get_settings
from app.db import get_session

router = APIRouter(prefix="/api/admin", tags=["admin-gitops"])


def require_execution(request: Request, settings: Settings) -> None:
    if blocker := service.execution_blocker(settings):
        raise HTTPException(503, blocker)
    if getattr(request.app.state, "cluster_git_backend", None) is None:
        raise HTTPException(503, {
            "code": "source_unavailable", "message": "Management repository backend is unavailable"
        })


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    acme_contact: str = Field(
        min_length=3, max_length=254,
        pattern=r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    )


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adopt: bool = False


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class ReaderInstalledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True)


@router.get("/clusters/{slug}/gitops")
async def status(
    slug: str, request: Request, _user=Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cluster = await service.cluster_by_slug(slug, session)
    result = await service.status_view(cluster, settings, session)
    if getattr(request.app.state, "cluster_git_backend", None) is None:
        result["can_preview"] = False
        result["blockers"].append({
            "code": "source_unavailable", "message": "Management repository backend is unavailable"
        })
    return result


@router.put("/clusters/{slug}/gitops")
async def edit(
    slug: str, req: DraftRequest, user=Depends(require_admin),
    settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cluster = await service.cluster_by_slug(slug, session)
    state = await service.save_settings(
        cluster, req.expected_version, req.acme_contact, settings, session
    )
    version = state.version
    await session.commit()
    audit_log(user["sub"], "cluster.gitops_settings", slug=slug, version=version)
    return {"version": version}


@router.post("/clusters/{slug}/gitops/preview", status_code=202)
async def preview(
    slug: str, req: PreviewRequest, request: Request, user=Depends(require_admin),
    settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_execution(request, settings)
    cluster = await service.cluster_by_slug(slug, session)
    operation = await service.enqueue_preview(cluster, req.adopt, user["sub"], settings, session)
    result = service.operation_view(operation)
    await session.commit()
    audit_log(user["sub"], "cluster.gitops_preview", slug=slug, operation_id=operation.id)
    return result


@router.post("/clusters/{slug}/gitops/publish", status_code=202)
async def publish(
    slug: str, req: PublishRequest, request: Request, user=Depends(require_admin),
    settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_execution(request, settings)
    cluster = await service.cluster_by_slug(slug, session)
    operation = await service.enqueue_publish(
        cluster, str(req.operation_id), settings, session, actor=user["sub"]
    )
    result = service.operation_view(operation)
    await session.commit()
    audit_log(user["sub"], "cluster.gitops_publish", slug=slug, operation_id=operation.id)
    return result


@router.get("/gitops-operations/{operation_id}")
async def operation_status(
    operation_id: UUID, _user=Depends(require_admin), settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    operation = await service.get_operation(str(operation_id), settings, session)
    return service.operation_view(operation)


@router.post("/clusters/{slug}/gitops/reader-installed")
async def reader_installed(
    slug: str, req: ReaderInstalledRequest, user=Depends(require_admin),
    settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cluster = await service.cluster_by_slug(slug, session)
    repository, state = await service.binding(cluster, settings, session, attach=True)
    if repository.reader_secret_version != req.version:
        raise HTTPException(
            409, "Reader credential changed; reload and verify the current version"
        )
    state.reader_installed_version = req.version
    await session.commit()
    audit_log(user["sub"], "cluster.reader_install_attested", slug=slug, version=req.version)
    return {"reader_installed_version": req.version}
