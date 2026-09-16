"""Repository-only tokens must not require account API permissions."""

import base64
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx
from fastapi import HTTPException

from app import gitops_git, repository_service
from app.config import Settings
from app.gitops_types import CustomerGitOpsError

REPO_URL = "https://platform.sunet.se/vdc/customer-sunet-clusters-test.git"
API_URL = "https://platform.sunet.se/api/v1/repos/vdc/customer-sunet-clusters-test"
TOKEN = "fixture-repository-only-token"


@pytest.fixture
def repository_document():
    return {
        "private": True,
        "full_name": "VDC/customer-sunet-clusters-test",
        "clone_url": REPO_URL,
        "permissions": {"pull": True, "push": True},
    }


async def test_repository_only_token_succeeds_without_requesting_user(
    monkeypatch, repository_document,
):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock(assert_all_called=False) as router:
        forbidden = router.get("https://platform.sunet.se/api/v1/user").respond(403)
        repository = router.get(API_URL).respond(200, json=repository_document)
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "platform-test-bot", TOKEN, Settings(cluster_environment="test")
        )
        assert status == "valid", message
        assert not forbidden.called
        assert repository.call_count == 1
        assert repository.calls.last.request.headers["Authorization"] == f"token {TOKEN}"
    read_access.assert_called_once_with(REPO_URL, "platform-test-bot", TOKEN)


@pytest.mark.parametrize("changes", [
    {"private": False}, {"private": "true"}, {"full_name": "VDC/other-customer"},
    {"clone_url": "https://other.example.test/vdc/customer-sunet-clusters-test.git"},
    {"clone_url": REPO_URL + "?token=" + TOKEN}, {"clone_url": None},
    {"permissions": {"pull": True, "push": False}}, {"permissions": {"push": "true"}},
    {"permissions": None},
])
async def test_repository_guards_run_before_git(
    monkeypatch, repository_document, changes,
):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router:
        router.get(API_URL).respond(200, json={**repository_document, **changes})
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "writer", TOKEN, Settings(cluster_environment="test")
        )
    assert status == "invalid"
    assert TOKEN not in message
    read_access.assert_not_called()


@pytest.mark.parametrize("code,expected", [
    (401, "invalid"), (403, "invalid"), (404, "invalid"), (302, "invalid"),
    (429, "error"), (500, "error"), (503, "error"),
])
async def test_api_errors_do_not_request_account_access_or_echo_bodies(
    monkeypatch, code, expected, caplog,
):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router:
        router.get(API_URL).respond(
            code, text=TOKEN, headers={"Location": f"https://other.example.test/{TOKEN}"}
        )
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "writer", TOKEN, Settings(cluster_environment="test")
        )
        assert len(router.calls) == 1
    assert status == expected
    assert f"HTTP {code}" in message
    assert TOKEN not in message + caplog.text
    read_access.assert_not_called()


@pytest.mark.parametrize("body", [TOKEN, "[]", "null"])
async def test_bad_repository_response_is_sanitized(monkeypatch, body, caplog):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router:
        router.get(API_URL).respond(200, text=body)
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "writer", TOKEN, Settings(cluster_environment="test")
        )
    assert status == "error"
    assert TOKEN not in message + caplog.text
    read_access.assert_not_called()


async def test_api_transport_error_is_sanitized(monkeypatch, caplog):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router:
        router.get(API_URL).mock(side_effect=httpx.ConnectError(TOKEN))
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "writer", TOKEN, Settings(cluster_environment="test")
        )
    assert status == "error"
    assert TOKEN not in message + caplog.text
    read_access.assert_not_called()


async def test_valid_api_permission_does_not_bypass_git_credentials(
    monkeypatch, repository_document, caplog,
):
    read_access = Mock(side_effect=CustomerGitOpsError(TOKEN, "repository_unavailable"))
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router:
        router.get(API_URL).respond(200, json=repository_document)
        status, message = await repository_service._validate_forgejo(
            REPO_URL, "wrong-username", TOKEN, Settings(cluster_environment="test")
        )
    assert status == "error" and "Git read access failed" in message
    assert TOKEN not in message + caplog.text
    read_access.assert_called_once_with(REPO_URL, "wrong-username", TOKEN)


async def test_disallowed_origin_is_rejected_before_any_network_access(monkeypatch):
    read_access = Mock()
    monkeypatch.setattr(repository_service, "check_repository_read_access", read_access)
    with respx.mock() as router, pytest.raises(HTTPException) as exc:
        await repository_service._validate_forgejo(
            "https://other.example.test/vdc/customer.git", "writer", TOKEN, Settings()
        )
    assert exc.value.status_code == 422
    assert not router.calls
    read_access.assert_not_called()


def git(directory: Path, *args: str, content: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=directory, input=content, capture_output=True, text=True,
        check=True, timeout=10,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("populated", [False, True])
def test_git_check_only_lists_refs_and_preserves_empty_or_populated_repositories(
    tmp_path, monkeypatch, populated,
):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    if populated:
        tree = git(remote, "hash-object", "-w", "-t", "tree", "--stdin", content="")
        commit = git(
            remote, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit-tree", tree, "-m", "Fixture",
        )
        git(remote, "update-ref", "refs/heads/main", commit)
    before = git(remote, "for-each-ref")
    monkeypatch.setattr(
        gitops_git, "_transport", lambda url: gitops_git.Transport(str(remote), "file")
    )
    run = gitops_git._run
    calls = []

    def capture(args, **kwargs):
        calls.append((args, kwargs))
        return run(args, **kwargs)

    monkeypatch.setattr(gitops_git, "_run", capture)
    assert gitops_git.check_repository_read_access(REPO_URL, "writer", TOKEN) is None
    assert git(remote, "for-each-ref") == before
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["ls-remote", "--refs", "--", str(remote)]
    assert kwargs["timeout"] == gitops_git.ACCESS_CHECK_TIMEOUT == 10
    assert TOKEN not in " ".join(args)
    env = kwargs["env"]
    config = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }
    expected = base64.b64encode(f"writer:{TOKEN}".encode()).decode()
    assert config[f"http.{REPO_URL}.extraHeader"] == f"Authorization: Basic {expected}"
    assert config["http.followRedirects"] == "false"
    assert config["http.sslVerify"] == "true"
    assert config["credential.helper"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("git", 10, output=TOKEN), OSError(TOKEN),
])
def test_git_timeout_and_execution_failure_are_sanitized(monkeypatch, error, caplog):
    run = Mock(side_effect=error)
    monkeypatch.setattr(gitops_git.subprocess, "run", run)
    with pytest.raises(CustomerGitOpsError) as exc:
        gitops_git.check_repository_read_access(REPO_URL, "writer", TOKEN)
    assert TOKEN not in str(exc.value) + caplog.text
    assert run.call_args.kwargs["timeout"] == 10
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL
