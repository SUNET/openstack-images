"""Pinned writer access must never follow a changed repository identity."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import repository_service
from app.config import Settings
from app.models import CustomerClusterRepository
from app.openbao_client import OpenBaoError, VersionedKVSecret


@pytest.fixture
def settings():
    return Settings(cluster_environment="test")


@pytest.fixture
def repository():
    return CustomerClusterRepository(
        id=1, customer_id=42, environment="test", version=5,
        repo_url="https://platform.sunet.se/vdc/customer.git",
        writer_username="writer", writer_secret_version=3,
    )


@pytest.fixture
def bao(monkeypatch, repository):
    client = AsyncMock()
    client.read_kv_secret_versioned.return_value = VersionedKVSecret(
        version=3,
        data={"username": "writer", "token": "SECRET", "repo_url": repository.repo_url},
    )
    monkeypatch.setattr(repository_service, "get_openbao", lambda: client)
    return client


async def test_worker_reads_only_pinned_version(repository, settings, bao):
    credentials = await repository_service.writer_credentials(repository, settings)
    assert credentials == ("writer", "SECRET")
    bao.read_kv_secret_versioned.assert_awaited_once_with(
        "kv/data/customer-cluster-repositories/42/test/writer", version=3
    )


@pytest.mark.parametrize("validation_status", ["unvalidated", "invalid", "error"])
async def test_recovery_can_read_current_pin_after_configuration_version_changes(
    repository, settings, bao, validation_status
):
    repository.version = 6
    repository.writer_secret_version = 4
    repository.validation_status = validation_status
    bao.read_kv_secret_versioned.return_value = VersionedKVSecret(
        version=4,
        data={"username": "writer", "token": "new-token", "repo_url": repository.repo_url},
    )
    credentials = await repository_service.writer_credentials(repository, settings)
    assert credentials == ("writer", "new-token")
    bao.read_kv_secret_versioned.assert_awaited_once_with(
        "kv/data/customer-cluster-repositories/42/test/writer", version=4
    )


@pytest.mark.parametrize("changes", [
    {"environment": "prod"}, {"writer_username": ""}, {"writer_secret_version": None},
    {"repo_url": "https://evil.test/vdc/customer.git"},
    {"repo_url": "https://platform.sunet.se/vdc/customer.git?token=SECRET"},
])
async def test_unbound_or_unpinned_writer_fails_before_read(repository, settings, bao, changes):
    for name, value in changes.items():
        setattr(repository, name, value)
    with pytest.raises(HTTPException) as exc:
        await repository_service.writer_credentials(repository, settings)
    assert "SECRET" not in str(exc.value)
    bao.read_kv_secret_versioned.assert_not_awaited()


@pytest.mark.parametrize("data", [
    {"username": "other", "token": "SECRET"},
    {"username": "writer", "token": " "},
    {"username": "writer", "token": "SECRET", "repo_url": "https://platform.sunet.se/o/other"},
    {"username": "writer", "token": "SECRET", "repo_url": "https://evil.test/o/repo"},
])
async def test_mismatched_stored_identity_is_sanitized(repository, settings, bao, data):
    bao.read_kv_secret_versioned.return_value = VersionedKVSecret(data=data, version=3)
    with pytest.raises(HTTPException) as exc:
        await repository_service.writer_credentials(repository, settings)
    assert exc.value.status_code == 409
    assert "SECRET" not in str(exc.value)


async def test_upstream_secret_error_is_sanitized(repository, settings, bao):
    bao.read_kv_secret_versioned.side_effect = OpenBaoError("SECRET")
    with pytest.raises(HTTPException) as exc:
        await repository_service.writer_credentials(repository, settings)
    assert exc.value.status_code == 503
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize(
    "environment", ["", "../prod", "test/prod", "TEST", "test\n", "dev", "staging", "production"]
)
def test_environment_is_explicit_and_path_safe(environment):
    with pytest.raises(HTTPException) as exc:
        repository_service.require_environment(
            Settings(cluster_environment=environment, is_test=True)
        )
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException):
        repository_service.repository_secret_path(42, environment, "writer")


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_deployment_environments_are_accepted(environment):
    settings = Settings(cluster_environment=environment)
    assert repository_service.require_environment(settings) == environment
    assert repository_service.repository_secret_path(42, environment, "reader") == (
        f"kv/data/customer-cluster-repositories/42/{environment}/reader"
    )


async def test_response_rejects_another_customers_repository(repository, settings):
    session = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await repository_service.repository_response(repository, 99, settings, session)
    assert exc.value.status_code == 409
    session.execute.assert_not_awaited()
