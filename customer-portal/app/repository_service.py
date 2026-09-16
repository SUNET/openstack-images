"""Customer/environment repository lifecycle, with pinned, identity-bound credentials.

Mutation helpers hold transaction-scoped locks but leave commit/rollback to their
caller. Workers share the repository advisory lock before reading or publishing.
"""

import asyncio
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.gitops_git import check_repository_read_access
from app.gitops_types import CustomerGitOpsError
from app.models import (
    ClusterGitOps,
    Contract,
    Customer,
    CustomerClusterRepository,
    GitOpsOperation,
    TenantCluster,
)
from app.openbao_client import (
    OpenBaoCASConflict,
    OpenBaoClient,
    OpenBaoError,
    VersionedKVSecret,
    get_openbao,
)
from app.repository_schemas import (
    CredentialErrorCode,
    CredentialKind,
    RepositoryClusterResponse,
    RepositoryCredentialError,
    RepositoryCredentialsRequest,
    RepositoryResponse,
    RepositoryUpdateRequest,
    canonical_repository_url,
)

_ACTIVE_OPERATIONS = ("queued", "running", "preview_ready")


@dataclass(frozen=True)
class CredentialReplacement:
    """Non-secret write receipt retained even if the database transaction fails."""

    repository: CustomerClusterRepository
    kind: CredentialKind
    previous_repository_version: int
    pinned_secret_version: int | None
    expected_secret_version: int
    written_secret_version: int

    def commit_failure(self) -> RepositoryCredentialError:
        return RepositoryCredentialError(
            code="credential_commit_failed",
            message=(
                f"OpenBao wrote secret version {self.written_secret_version}, but the database "
                "commit could not be confirmed. Reload repository status before explicitly "
                f"retrying with expected_secret_version={self.written_secret_version}."
            ),
            kind=self.kind,
            repository_version=self.previous_repository_version,
            pinned_secret_version=self.pinned_secret_version,
            expected_secret_version=self.expected_secret_version,
            written_secret_version=self.written_secret_version,
        )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def require_environment(settings: Settings) -> str:
    environment = settings.cluster_environment
    if environment not in ("test", "prod"):
        raise HTTPException(503, "Cluster repository environment must be explicitly test or prod")
    return environment


def repository_secret_path(customer_id: int, environment: str, kind: CredentialKind) -> str:
    """Construct only the existing, customer-scoped KV namespace."""
    if (
        type(customer_id) is not int or customer_id <= 0
        or environment not in ("test", "prod") or kind not in ("writer", "reader")
    ):
        raise HTTPException(409, "Repository secret namespace is invalid")
    return f"kv/data/customer-cluster-repositories/{customer_id}/{environment}/{kind}"


def _canonical_url(value: str, settings: Settings) -> str:
    try:
        canonical_repository_url(
            f"{settings.customer_repository_origin.rstrip('/')}/owner/repository",
            settings.customer_repository_origin,
        )
    except ValueError:
        raise HTTPException(
            503, "Customer repository origin is not configured correctly"
        ) from None
    try:
        return canonical_repository_url(value, settings.customer_repository_origin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _check_repository(repository: CustomerClusterRepository, settings: Settings) -> str:
    if repository.environment != require_environment(settings):
        raise HTTPException(409, "Repository belongs to a different environment")
    return _canonical_url(repository.repo_url, settings)


async def lock_repository(session: AsyncSession, repository_id: int) -> None:
    """Shared lock protocol with GitOps preview/publication workers."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('customer-repository'), :repository_id)"),
        {"repository_id": repository_id},
    )


async def get_repository(
    customer_id: int,
    settings: Settings,
    session: AsyncSession,
    *,
    lock: bool = False,
) -> CustomerClusterRepository | None:
    environment = require_environment(settings)
    customer_query = select(Customer.id).where(Customer.id == customer_id)
    if lock:
        # Also serialize against customer domain changes/deletion in admin.py.
        customer_query = customer_query.with_for_update()
    if (await session.execute(customer_query)).scalar_one_or_none() is None:
        raise HTTPException(404, "Customer not found")
    if lock:
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('customer-repository-owner'), hashtext(:owner))"
            ),
            {"owner": f"{customer_id}/{environment}"},
        )
    query = select(CustomerClusterRepository).where(
        CustomerClusterRepository.customer_id == customer_id,
        CustomerClusterRepository.environment == environment,
    )
    repository = (await session.execute(query)).scalar_one_or_none()
    if lock and repository is not None:
        await lock_repository(session, repository.id)
        repository = (
            await session.execute(query.execution_options(populate_existing=True))
        ).scalar_one()
    return repository


def check_version(repository: CustomerClusterRepository | None, expected_version: int) -> None:
    if expected_version != (repository.version if repository is not None else 0):
        raise HTTPException(409, "Repository configuration changed; reload before retrying")


async def _check_binding(
    repo_url: str, customer_id: int, settings: Settings, session: AsyncSession
) -> None:
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtext('customer-repository-url'), hashtext(:repo_url))"
        ),
        {"repo_url": repo_url},
    )
    # Normalize legacy addresses too: their case and .git suffix were not canonicalized.
    other_urls = (
        await session.execute(
            select(CustomerClusterRepository.repo_url).where(
                or_(
                    CustomerClusterRepository.customer_id != customer_id,
                    CustomerClusterRepository.environment != require_environment(settings),
                )
            )
        )
    ).scalars()
    for other_url in other_urls:
        try:
            canonical = canonical_repository_url(other_url, settings.customer_repository_origin)
        except ValueError:
            continue
        if canonical == repo_url:
            raise HTTPException(
                409, "Repository is already associated with another customer or environment"
            )


def _invalidate_validation(repository: CustomerClusterRepository) -> None:
    repository.validation_status = "unvalidated"
    repository.validation_message = None
    repository.validated_at = None


async def save_repository(
    customer_id: int,
    request: RepositoryUpdateRequest,
    settings: Settings,
    session: AsyncSession,
) -> CustomerClusterRepository:
    repository = await get_repository(customer_id, settings, session, lock=True)
    check_version(repository, request.expected_version)
    repo_url = _canonical_url(request.repo_url, settings)
    await _check_binding(repo_url, customer_id, settings, session)
    if repository is None:
        repository = CustomerClusterRepository(
            customer_id=customer_id,
            environment=require_environment(settings),
            repo_url=repo_url,
            writer_username="",
            version=1,
            validation_status="unvalidated",
        )
        session.add(repository)
        await session.flush()
        await lock_repository(session, repository.id)
        return repository

    try:
        previous_url = canonical_repository_url(repository.repo_url)
    except ValueError:
        previous_url = repository.repo_url
    if previous_url == repo_url:
        # Persisted previews bind the original spelling, even for equivalent
        # legacy Forgejo owner casing. Do not invalidate publication recovery.
        return repository

    published = await session.scalar(
        select(ClusterGitOps.cluster_id).where(
            ClusterGitOps.repository_id == repository.id,
            or_(ClusterGitOps.published_at.is_not(None), ClusterGitOps.last_commit.is_not(None)),
        ).limit(1)
    )
    operation = await session.scalar(
        select(GitOpsOperation.id).where(
            GitOpsOperation.repository_id == repository.id,
            or_(
                # A failed/ambiguous publication may already have reached the remote.
                GitOpsOperation.kind == "publish",
                GitOpsOperation.status.in_(_ACTIVE_OPERATIONS),
                GitOpsOperation.result_commit.is_not(None),
            ),
        ).limit(1)
    )
    if published is not None or operation is not None:
        raise HTTPException(
            409, "Repository URL is locked by publication intent or an active operation"
        )
    repository.repo_url = repo_url
    repository.writer_username = ""
    repository.reader_username = None
    repository.writer_secret_version = None
    repository.reader_secret_version = None
    repository.writer_updated_at = None
    repository.reader_updated_at = None
    repository.version += 1
    _invalidate_validation(repository)
    return repository


def _secret_client() -> OpenBaoClient:
    try:
        return get_openbao()
    except RuntimeError:
        raise HTTPException(503, "Repository credential storage is unavailable") from None


async def _latest_writer_version(client: OpenBaoClient, path: str) -> int:
    try:
        return (await client.read_kv_secret_versioned(path)).version
    except OpenBaoError as exc:
        if exc.status_code == 404:
            return 0
        raise


def _credential_conflict(
    repository: CustomerClusterRepository,
    kind: CredentialKind,
    code: CredentialErrorCode,
    message: str,
    *,
    cas: int | None = None,
    latest: int | None = None,
) -> HTTPException:
    detail = RepositoryCredentialError(
        code=code,
        message=message,
        kind=kind,
        repository_version=repository.version,
        pinned_secret_version=getattr(repository, f"{kind}_secret_version"),
        expected_secret_version=cas,
        latest_secret_version=latest,
    )
    return HTTPException(409, detail.model_dump())


async def replace_credentials(
    customer_id: int,
    kind: CredentialKind,
    request: RepositoryCredentialsRequest,
    settings: Settings,
    session: AsyncSession,
) -> CredentialReplacement:
    """Replace using the DB pin, or an explicitly confirmed newer CAS version.

    Reader replacements are compared with the configured writer. Writer replacement
    cannot inspect the write-only reader secret: token separation and Forgejo token
    scope must also be verified manually by the operator.
    """
    repository = await get_repository(customer_id, settings, session, lock=True)
    if repository is None:
        raise HTTPException(409, "Configure the customer repository first")
    check_version(repository, request.expected_version)
    repo_url = _check_repository(repository, settings)
    await _check_binding(repo_url, customer_id, settings, session)
    path = repository_secret_path(customer_id, repository.environment, kind)
    pinned = getattr(repository, f"{kind}_secret_version")
    cas = request.expected_secret_version
    if cas is None:
        cas = pinned
    if pinned is not None and cas < pinned:
        raise _credential_conflict(
            repository, kind, "secret_version_regression",
            f"expected_secret_version cannot be older than the pinned secret version {pinned}",
            cas=cas,
        )
    if kind == "reader" and cas is None and repository.reader_username:
        raise _credential_conflict(
            repository, kind, "secret_version_confirmation_required",
            "Legacy reader replacement requires an explicit expected_secret_version "
            "obtained from OpenBao by an operator",
        )
    if kind == "reader" and repository.writer_username:
        writer = await _writer_secret(repository, settings, allow_legacy=True)
        if hmac.compare_digest(request.token.get_secret_value(), writer.data["token"]):
            raise HTTPException(
                409, "Reader token must differ from the configured writer token; "
                "verify reader-only token scope manually in Forgejo",
            )
    client = _secret_client()
    try:
        if kind == "writer" and cas is None:
            latest = await _latest_writer_version(client, path)
            if latest != 0:
                raise _credential_conflict(
                    repository, kind, "secret_version_confirmation_required",
                    f"Existing writer secret version {latest} requires explicit "
                    "expected_secret_version confirmation before replacement",
                    latest=latest,
                )
            cas = 0
        if cas is None:
            cas = 0
        secret_version = await client.write_kv_secret(
            path,
            {
                "username": request.username,
                "token": request.token.get_secret_value(),
                "repo_url": repo_url,
            },
            cas=cas,
        )
    except OpenBaoCASConflict:
        latest = None
        if kind == "writer":
            try:
                latest = await _latest_writer_version(client, path)
            except OpenBaoError:
                # Keep the CAS conflict actionable even if the metadata lookup fails.
                latest = None
        observed = f"; observed writer version {latest}" if latest is not None else ""
        raise _credential_conflict(
            repository, kind, "secret_version_conflict",
            f"Secret version conflict: pinned {pinned}, attempted CAS {cas}{observed}. "
            "Confirm the current OpenBao version and supply expected_secret_version explicitly.",
            cas=cas, latest=latest,
        ) from None
    except OpenBaoError:
        raise HTTPException(503, "Repository credential storage failed") from None
    # Old KV versions remain readable if the database transaction fails to commit.
    setattr(repository, f"{kind}_username", request.username)
    setattr(repository, f"{kind}_secret_version", secret_version)
    setattr(repository, f"{kind}_updated_at", _now())
    repository.version += 1
    _invalidate_validation(repository)
    return CredentialReplacement(
        repository=repository,
        kind=kind,
        previous_repository_version=request.expected_version,
        pinned_secret_version=pinned,
        expected_secret_version=cas,
        written_secret_version=secret_version,
    )


async def _writer_secret(
    repository: CustomerClusterRepository, settings: Settings, *, allow_legacy: bool = False
) -> VersionedKVSecret:
    repo_url = _check_repository(repository, settings)
    if not repository.writer_username:
        raise HTTPException(409, "Repository writer credentials are not configured")
    if repository.writer_secret_version is None and not allow_legacy:
        raise HTTPException(
            409, "Validate or replace legacy writer credentials before publication"
        )
    path = repository_secret_path(repository.customer_id, repository.environment, "writer")
    try:
        secret = await _secret_client().read_kv_secret_versioned(
            path, version=repository.writer_secret_version
        )
    except OpenBaoError:
        raise HTTPException(503, "Repository writer credentials are unavailable") from None
    data = secret.data
    try:
        credentials = RepositoryCredentialsRequest(
            expected_version=repository.version,
            username=data.get("username", ""),
            token=data.get("token", ""),
        )
    except ValidationError:
        raise HTTPException(409, "Stored repository writer credentials are invalid") from None
    if credentials.username != repository.writer_username:
        raise HTTPException(409, "Stored repository writer identity does not match configuration")
    if "repo_url" in data:
        try:
            bound_url = canonical_repository_url(
                data["repo_url"], settings.customer_repository_origin
            )
        except ValueError:
            raise HTTPException(
                409, "Stored writer credential repository binding is invalid"
            ) from None
        if bound_url != repo_url:
            raise HTTPException(409, "Stored writer credentials belong to a different repository")
    return secret


async def writer_credentials(
    repository: CustomerClusterRepository, settings: Settings
) -> tuple[str, str]:
    """Read the active DB-pinned writer while the caller holds the repository lock.

    The caller enforces publication/recovery eligibility. Credential identity,
    repository binding, and deployment environment are checked here.
    """
    secret = await _writer_secret(repository, settings)
    return secret.data["username"], secret.data["token"]


def _api_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _validate_forgejo(
    repo_url: str, username: str, token: str, settings: Settings
) -> tuple[str, str]:
    """Use repository-scoped API evidence and the actual Git credential pair.

    Specific-repository tokens need no access to /user. Successful validation
    proves read access, not that a later write can bypass branch protection.
    """
    repo_url = _canonical_url(repo_url, settings)
    parsed = urlsplit(repo_url)
    identity = parsed.path.removeprefix("/").removesuffix(".git")
    api_origin = f"{parsed.scheme}://{parsed.netloc}/api/v1"
    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False, trust_env=False,
            headers={"Authorization": f"token {token}"},
        ) as client:
            response = await client.get(f"{api_origin}/repos/{identity}")
    except httpx.RequestError:
        return "error", "Forgejo API is unavailable"
    if response.status_code != 200:
        status = (
            "error" if response.status_code == 429 or response.status_code >= 500 else "invalid"
        )
        return status, (
            "Forgejo API could not verify access to the configured repository "
            f"(HTTP {response.status_code})"
        )
    body = _api_object(response)
    if body is None:
        return "error", "Forgejo API returned an invalid repository response"
    if body.get("private") is not True:
        return "invalid", "Forgejo API did not confirm a private repository"
    if not isinstance(body.get("full_name"), str) or body["full_name"].lower() != identity:
        return "invalid", "Forgejo API repository identity does not match configuration"
    try:
        clone_url = body.get("clone_url")
        if not isinstance(clone_url, str) or canonical_repository_url(
            clone_url, settings.customer_repository_origin
        ) != repo_url:
            return "invalid", "Forgejo API repository URL does not match configuration"
    except ValueError:
        return "invalid", "Forgejo API repository URL does not match configuration"
    permissions = body.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        return "invalid", "Forgejo API did not confirm the writer user's push permission"
    try:
        await asyncio.to_thread(check_repository_read_access, repo_url, username, token)
    except CustomerGitOpsError:
        return "error", (
            "Git read access failed; check the stored username/token and repository availability"
        )
    return "valid", (
        "Repository API confirms private repository identity and user push permission; "
        "Git read access succeeds with the supplied credentials. This is not proof of token scope "
        "or Git write capability; publication checks write authorization and branch protection."
    )


async def validate_repository(
    customer_id: int, expected_version: int, settings: Settings, session: AsyncSession
) -> CustomerClusterRepository:
    repository = await get_repository(customer_id, settings, session, lock=True)
    if repository is None:
        raise HTTPException(409, "Configure the customer repository first")
    check_version(repository, expected_version)
    repo_url = _check_repository(repository, settings)
    await _check_binding(repo_url, customer_id, settings, session)
    secret = await _writer_secret(repository, settings, allow_legacy=True)
    status, message = await _validate_forgejo(
        repo_url, secret.data["username"], secret.data["token"], settings
    )
    if status == "valid" and repository.writer_secret_version is None:
        repository.writer_secret_version = secret.version
        repository.version += 1
        repository.writer_updated_at = _now()
    repository.validation_status = status
    repository.validation_message = message
    repository.validated_at = _now()
    return repository


async def repository_response(
    repository: CustomerClusterRepository | None,
    customer_id: int,
    settings: Settings,
    session: AsyncSession,
) -> RepositoryResponse:
    environment = require_environment(settings)
    if repository is None:
        return RepositoryResponse(
            customer_id=customer_id,
            environment=environment,
            bases_revision=settings.customer_cluster_bases_revision,
        )
    if repository.customer_id != customer_id or repository.environment != environment:
        raise HTTPException(409, "Repository ownership does not match the requested customer")
    clusters = await session.execute(
        select(TenantCluster.slug, TenantCluster.name, ClusterGitOps.reader_installed_version)
        .join(ClusterGitOps, ClusterGitOps.cluster_id == TenantCluster.id)
        .join(Contract, Contract.id == TenantCluster.contract_id)
        .where(
            ClusterGitOps.repository_id == repository.id,
            ClusterGitOps.environment == environment,
            Contract.customer_id == customer_id,
        )
        .order_by(TenantCluster.slug)
    )
    return RepositoryResponse(
        customer_id=customer_id,
        environment=environment,
        configured=True,
        id=repository.id,
        version=repository.version,
        repo_url=_check_repository(repository, settings),
        writer_username=repository.writer_username,
        reader_username=repository.reader_username,
        writer_configured=bool(repository.writer_username),
        reader_configured=bool(repository.reader_username),
        writer_secret_version=repository.writer_secret_version,
        reader_secret_version=repository.reader_secret_version,
        writer_updated_at=repository.writer_updated_at,
        reader_updated_at=repository.reader_updated_at,
        validation_status=repository.validation_status,
        validation_message=repository.validation_message,
        validated_at=repository.validated_at,
        clusters=[RepositoryClusterResponse(**row._mapping) for row in clusters],
        bases_revision=settings.customer_cluster_bases_revision,
    )
