"""Repository identity normalization and explicit credential replacement boundaries."""

import pytest
from pydantic import SecretStr, ValidationError

from app.repository_schemas import (
    RepositoryCredentialsRequest,
    RepositoryUpdateRequest,
    canonical_repository_url,
)


@pytest.mark.parametrize("value", [
    "https://platform.sunet.se/VDC/Customer",
    "https://PLATFORM.SUNET.SE:443/vdc/customer.git",
    "https://platform.sunet.se/vdc/customer.GIT/",
])
def test_repository_identity_has_one_canonical_spelling(value):
    assert canonical_repository_url(value, "https://platform.sunet.se") == (
        "https://platform.sunet.se/vdc/customer.git"
    )


@pytest.mark.parametrize("value", [
    "", " ", " https://platform.sunet.se/vdc/repo", "http://platform.sunet.se/vdc/repo",
    "https://platform.sunet.se/vdc/repo\n", "https://platform.sunet.se/vdc/\trepo",
    "https://user:secret@platform.sunet.se/vdc/repo", "https://platform.sunet.se/vdc/repo?",
    "https://platform.sunet.se/vdc/repo#", "https://platform.sunet.se/vdc/repo?token=secret",
    "https://platform.sunet.se/vdc/repo%2fother", "https://platform.sunet.se/%76dc/repo",
    "https://platform.sunet.se/vdc/../repo", "https://platform.sunet.se/vdc//repo",
    "https://platform.sunet.se/vdc/repo//", "https://platform.sunet.se/vdc/repo;other",
    "https://platform.sunet.se\\evil.test/vdc/repo", "https://platform.sunet.se/vdc/.git",
    "https://platform.sunet.se/vdc/..", "https://platform.sunet.se/vdc",
    "https://evil.test/vdc/repo", "https://platform.sunet.se.evil.test/vdc/repo",
    "https://platform.sunet.se:8443/vdc/repo", "https://platform.sunet.se./vdc/repo",
    "https://platform.sunet.se:/vdc/repo", "https://platform.sunet.se:invalid/vdc/repo",
])
def test_ambiguous_or_untrusted_urls_rejected(value):
    with pytest.raises(ValueError):
        canonical_repository_url(value, "https://platform.sunet.se")


@pytest.mark.parametrize("token", ["", " ", "\n", "token\r\nheader", " token", "token ", None])
def test_credential_replacement_rejects_blank_or_unsafe_tokens(token):
    with pytest.raises(ValidationError):
        RepositoryCredentialsRequest(expected_version=1, username="writer", token=token)


@pytest.mark.parametrize("username", ["", " ", "writer\n", "writer:token", None])
def test_credential_replacement_rejects_blank_or_unsafe_usernames(username):
    with pytest.raises(ValidationError):
        RepositoryCredentialsRequest(expected_version=1, username=username, token="secret")


def test_token_is_write_only_and_replacement_is_explicit():
    request = RepositoryCredentialsRequest(expected_version=1, username="writer", token="secret")
    assert isinstance(request.token, SecretStr)
    assert "secret" not in repr(request).replace("expected_secret_version", "")
    assert '"token":"**********"' in request.model_dump_json()
    with pytest.raises(ValidationError):
        RepositoryCredentialsRequest(expected_version=1, username="writer")
    with pytest.raises(ValidationError):
        RepositoryUpdateRequest(
            expected_version=1, repo_url="https://platform.sunet.se/vdc/repo", token="secret"
        )


@pytest.mark.parametrize("version", [True, "1", -1, 1.5, None])
def test_optimistic_versions_are_strict_integers(version):
    with pytest.raises(ValidationError):
        RepositoryUpdateRequest(expected_version=version, repo_url="https://platform.sunet.se/o/r")


@pytest.mark.parametrize("version", [True, "1", -1, 1.5])
def test_recovery_confirmation_is_a_strict_nonnegative_version(version):
    with pytest.raises(ValidationError):
        RepositoryCredentialsRequest(
            expected_version=1, username="writer", token="secret", expected_secret_version=version
        )
