"""Non-secret repository views and explicit, write-only credential replacement."""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

CredentialKind = Literal["writer", "reader"]
CredentialErrorCode = Literal[
    "secret_version_confirmation_required",
    "secret_version_conflict",
    "secret_version_regression",
    "credential_commit_failed",
]
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _https_origin(value: str) -> str:
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("Repository URL must be an HTTPS URL on the configured origin")
    if any(char in value for char in ("%", "\\", "?", "#")):
        raise ValueError("Repository URL contains disallowed URL components")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Repository URL is invalid") from None
    if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc:
        raise ValueError("Repository URL must be HTTPS without embedded credentials")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parsed.hostname):
        raise ValueError("Repository URL host is invalid")
    if parsed.netloc.endswith(":"):
        raise ValueError("Repository URL port is invalid")
    suffix = f":{port}" if port not in (None, 443) else ""
    return f"https://{parsed.hostname.lower()}{suffix}"


def canonical_repository_url(value: str, allowed_origin: str | None = None) -> str:
    """Forgejo owner/repository identities are case-insensitive; reject encoded paths."""
    origin = _https_origin(value)
    if allowed_origin is not None:
        expected = _https_origin(allowed_origin)
        if urlsplit(allowed_origin).path not in ("", "/"):
            raise ValueError("Configured repository origin must not contain a path")
        if origin != expected:
            raise ValueError("Repository URL must use the configured HTTPS origin")
    path = urlsplit(value).path.removesuffix("/")
    parts = path.split("/")
    if len(parts) != 3 or parts[0]:
        raise ValueError("Repository URL must identify exactly one owner and repository")
    owner, repository = parts[1:]
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not _IDENTITY.fullmatch(owner) or not _IDENTITY.fullmatch(repository):
        raise ValueError("Repository owner or name is invalid")
    return f"{origin}/{owner.lower()}/{repository.lower()}.git"


class RepositoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    repo_url: str = Field(min_length=1, max_length=2048)
    expected_version: int = Field(ge=0, strict=True)

    @field_validator("repo_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return canonical_repository_url(value)


class RepositoryCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_version: int = Field(ge=1, strict=True)
    username: str = Field(min_length=1, max_length=255)
    token: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    expected_secret_version: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description=(
            "Explicitly confirmed current OpenBao CAS version for migration or recovery; "
            "must not be older than the pinned version. Omission uses the database pin."
        ),
    )

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("Credential username must be a nonblank Forgejo username")
        return value

    @field_validator("token")
    @classmethod
    def nonblank_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value() or any(
            ord(char) < 33 or ord(char) > 126 for char in value.get_secret_value()
        ):
            raise ValueError("Credential token must be nonblank and contain no whitespace")
        return value


class RepositoryCredentialError(BaseModel):
    """Recovery metadata only; a failed commit requires reloading the database state."""

    model_config = ConfigDict(extra="forbid")

    code: CredentialErrorCode
    message: str
    kind: CredentialKind
    repository_version: int = Field(
        description="Last confirmed configuration version at attempt start"
    )
    pinned_secret_version: int | None = Field(
        description="Last confirmed database pin at attempt start; reload after commit failure"
    )
    expected_secret_version: int | None = None
    latest_secret_version: int | None = Field(
        default=None, description="Observed writer KV version; reader KV is never read"
    )
    written_secret_version: int | None = Field(
        default=None, description="Confirmed KV write version, not proof of a database commit"
    )


class RepositoryValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_version: int = Field(ge=1, strict=True)


class RepositoryClusterResponse(BaseModel):
    slug: str
    name: str
    reader_installed_version: int | None = None


class RepositoryResponse(BaseModel):
    customer_id: int
    environment: str
    configured: bool = False
    id: int | None = None
    version: int = 0
    repo_url: str | None = None
    writer_username: str = ""
    reader_username: str | None = None
    writer_configured: bool = False
    reader_configured: bool = False
    writer_secret_version: int | None = None
    reader_secret_version: int | None = None
    writer_updated_at: datetime | None = None
    reader_updated_at: datetime | None = None
    validation_status: str = "unvalidated"
    validation_message: str | None = None
    validated_at: datetime | None = None
    clusters: list[RepositoryClusterResponse] = Field(default_factory=list)
    bases_revision: str = Field(
        description="Approved configured default; an existing repository baseline may differ"
    )
