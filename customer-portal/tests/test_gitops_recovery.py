"""Read-only recovery and last-moment upstream gates using disposable local Git."""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from typing import Any

import pytest

from app import customer_gitops, gitops_git
from app.customer_gitops import CustomerGitOpsError, recover_preview
from app.gitops_git import GitRepository, TreeEntry
from tests.test_gitops_publisher import (
    APPS,
    BASES_URL,
    INGRESS,
    REPO_URL,
    TOKEN,
    USERNAME,
    Repositories,
    commit,
    git,
)
from tests.test_gitops_publisher import repos as repos


def recover(repos: Repositories, preview: dict[str, Any], **kwargs: Any) -> str | None:
    return recover_preview(
        repo_url=kwargs.pop("repo_url", REPO_URL),
        username=USERNAME,
        token=TOKEN,
        preview=preview,
        operation_id=kwargs.pop("operation_id", "operation-001"),
        settings=kwargs.pop("settings", repos.settings),
        **kwargs,
    )


@pytest.mark.parametrize("state", ["changed", "deleted", "suspended"])
@pytest.mark.parametrize("initial", [False, True])
def test_before_push_blocks_upstream_changes_after_validation_and_commit_generation(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    initial: bool,
) -> None:
    baseline = {} if initial else repos.tree()
    original_head = None if initial else repos.seed()
    preview = repos.preview(repos.tree(acme_contact="updated@example.test"), baseline=baseline)
    events = []
    upstream = {"state": "ready"}
    blocked = CustomerGitOpsError(f"Upstream cluster {state}", f"source_{state}")
    original_validate = customer_gitops.validate_kustomize
    original_commit = GitRepository.commit

    def validate(*args: Any, **kwargs: Any) -> Any:
        result = original_validate(*args, **kwargs)
        events.append("validated")
        return result

    def create_commit(self: GitRepository, **kwargs: Any) -> str:
        sha = original_commit(self, **kwargs)
        assert self.run(["cat-file", "-t", sha]).stdout.strip() == b"commit"
        events.append("committed-locally")
        upstream["state"] = state
        return sha

    def assert_fresh() -> None:
        events.append("before-push")
        assert upstream["state"] == state
        raise blocked

    def unexpected_push(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("A failed upstream gate must prevent every push")

    monkeypatch.setattr(customer_gitops, "validate_kustomize", validate)
    monkeypatch.setattr(GitRepository, "commit", create_commit)
    monkeypatch.setattr(GitRepository, "push", unexpected_push)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview, before_push=assert_fresh)
    assert error.value is blocked
    assert events == ["validated", "committed-locally", "before-push"]
    assert git(repos.customer, "rev-parse", "--verify", "main", check=False) == (
        original_head or ""
    )


def test_before_push_is_rechecked_on_each_writing_retry_but_not_confirmation(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = repos.preview()
    callbacks = []
    pushes = []
    upstream = {"ready": True}
    blocked = CustomerGitOpsError("Cluster was suspended during retry", "source_suspended")
    original_push = GitRepository.push

    def before_push() -> None:
        callbacks.append(upstream["ready"])
        if not upstream["ready"]:
            raise blocked

    def reject_first(self: GitRepository, sha: str, expected: str | None) -> bool:
        pushes.append(sha)
        return False if len(pushes) == 1 else original_push(self, sha, expected)

    monkeypatch.setattr(GitRepository, "push", reject_first)
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview, before_push=before_push)
    assert error.value.code == "push_failed"
    upstream["ready"] = False
    with pytest.raises(CustomerGitOpsError) as error:
        repos.publish(preview, before_push=before_push)
    assert error.value is blocked
    assert len(pushes) == 1
    assert git(repos.customer, "show-ref", check=False) == ""
    upstream["ready"] = True
    sha = repos.publish(preview, before_push=before_push)
    assert len(pushes) == 2
    assert callbacks == [True, False, True]
    upstream["ready"] = False
    assert repos.publish(preview, before_push=before_push) == sha
    assert callbacks == [True, False, True]
    assert len(pushes) == 2


@pytest.mark.parametrize(
    "bases_url,revision",
    [
        ("https://new.example.test/public/other-bases.git", "f" * 40),
        (BASES_URL, "main"),
        ("http://invalid-default.example.test/public/bases.git", ""),
    ],
)
def test_recovery_ignores_runtime_defaults_and_does_only_customer_git_reads(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    bases_url: str,
    revision: str,
) -> None:
    preview = repos.preview()
    sha = repos.publish(preview)
    recorded = json.dumps(preview, sort_keys=True)
    settings = replace(
        repos.settings,
        customer_cluster_bases_url=bases_url,
        customer_cluster_bases_revision=revision,
        git_author_name="",
        git_author_email="",
    )
    calls = []
    transports = []
    original_run = gitops_git._run
    original_transport = gitops_git._transport

    def read_only(args: list[str], **kwargs: Any) -> Any:
        calls.append(args[0])
        assert args[0] not in {
            "push",
            "commit-tree",
            "update-index",
            "hash-object",
            "write-tree",
            "read-tree",
        }
        return original_run(args, **kwargs)

    def transport(url: str) -> Any:
        transports.append(url)
        assert url == REPO_URL
        return original_transport(url)

    def unexpected_validation(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Recovery must not consult runtime defaults, fetch bases or run a build")

    monkeypatch.setattr(gitops_git, "_run", read_only)
    monkeypatch.setattr(gitops_git, "_transport", transport)
    monkeypatch.setattr(customer_gitops, "_configured_url", unexpected_validation)
    monkeypatch.setattr(customer_gitops, "validate_kustomize", unexpected_validation)
    monkeypatch.setattr(GitRepository, "public_base", unexpected_validation)
    assert recover(repos, preview, settings=settings) == sha
    assert "recover_preview" in customer_gitops.__all__
    assert calls.count("ls-remote") >= 2
    assert transports == [REPO_URL]
    assert git(repos.customer, "rev-list", "--count", "main") == "1"
    assert json.dumps(preview, sort_keys=True) == recorded


@pytest.mark.parametrize("repos", ["sha1", "sha256"], indirect=True)
def test_recovery_accepts_an_approved_noop_without_an_operation_marker(
    repos: Repositories,
) -> None:
    sha = repos.seed()
    preview = repos.preview(adopt=True)
    assert "Customer-GitOps-Operation" not in git(repos.customer, "log", "-1", "--format=%B")
    assert recover(repos, preview) == sha
    assert git(repos.customer, "rev-list", "--count", "main") == "1"


def test_recovery_after_a_lost_confirmation_survives_runtime_base_changes(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = repos.preview()

    def lose_confirmation(*args: Any, **kwargs: Any) -> str:
        raise CustomerGitOpsError(
            "Confirmation transport is unavailable", "repository_unavailable"
        )

    with monkeypatch.context() as unavailable:
        unavailable.setattr(customer_gitops, "_confirm", lose_confirmation)
        with pytest.raises(CustomerGitOpsError) as error:
            repos.publish(preview)
    assert error.value.code == "push_unconfirmed"
    sha = git(repos.customer, "rev-parse", "main")
    message = git(repos.customer, "show", "-s", "--format=%B", sha)
    assert "Customer-GitOps-Operation: operation-001" in message
    assert preview["fingerprint"] in message
    settings = replace(
        repos.settings,
        customer_cluster_bases_url="https://new.example.test/public/bases.git",
        customer_cluster_bases_revision="b" * 40,
    )
    assert recover(repos, preview, settings=settings) == sha
    assert git(repos.customer, "rev-list", "--count", "main") == "1"


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "readme-only",
        "manifest",
        "invalid-yaml",
        "deleted-manifest",
        "gitlink",
        "bases-url",
        "missing-gitmodules",
        "symlink",
        "missing-main",
        "other-repository",
    ],
)
def test_recovery_returns_none_for_state_that_does_not_match_the_approved_preview(
    repos: Repositories,
    change: str,
) -> None:
    preview = repos.preview()
    if change == "readme-only":
        commit(repos.customer, {"README.md": "Customer introduction"})
    elif change != "empty":
        sha = repos.publish(preview)
        if change == "manifest":
            commit(
                repos.customer,
                {
                    f"{INGRESS}/issuer.yaml": repos.tree(acme_contact="changed@example.test")[
                        f"{INGRESS}/issuer.yaml"
                    ]
                },
            )
        elif change == "invalid-yaml":
            commit(repos.customer, {f"{INGRESS}/issuer.yaml": "[invalid YAML"})
        elif change == "deleted-manifest":
            commit(repos.customer, {}, remove=(f"{INGRESS}/issuer.yaml",))
        elif change == "gitlink":
            commit(repos.customer, {"k8s-manifests": ("160000", "a" * 40)})
        elif change == "bases-url":
            commit(
                repos.customer,
                {
                    ".gitmodules": repos.tree()[".gitmodules"].replace(
                        BASES_URL, "https://other.example.test/public/bases.git"
                    )
                },
            )
        elif change == "missing-gitmodules":
            commit(repos.customer, {}, remove=(".gitmodules",))
        elif change == "symlink":
            commit(repos.customer, {f"{INGRESS}/issuer.yaml": ("120000", "../../outside")})
        elif change == "other-repository":
            commit(
                repos.customer,
                {
                    f"{APPS}/argocd.yaml": repos.tree(
                        repo_url="https://forgejo.example.test/other/customer.git"
                    )[f"{APPS}/argocd.yaml"]
                },
            )
        else:
            git(repos.customer, "update-ref", "refs/tags/previous", sha)
            git(repos.customer, "update-ref", "-d", "refs/heads/main")
    before = git(repos.customer, "show-ref", check=False)
    assert recover(repos, preview) is None
    assert git(repos.customer, "show-ref", check=False) == before


@pytest.mark.parametrize("overlap", [False, True])
def test_recovery_rechecks_main_if_it_moves_during_confirmation(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    overlap: bool,
) -> None:
    preview = repos.preview()
    repos.publish(preview)
    advanced = []
    original = GitRepository.remote_head

    def advance(self: GitRepository) -> str | None:
        if not advanced:
            files = {"README.md": "Updated documentation", "notes.txt": "Harmless customer change"}
            if overlap:
                files[f"{INGRESS}/issuer.yaml"] = repos.tree(acme_contact="changed@example.test")[
                    f"{INGRESS}/issuer.yaml"
                ]
            advanced.append(commit(repos.customer, files))
        return original(self)

    monkeypatch.setattr(GitRepository, "remote_head", advance)
    result = recover(repos, preview)
    assert result == (None if overlap else advanced[0])
    assert git(repos.customer, "rev-parse", "main") == advanced[0]


@pytest.mark.parametrize("phase", ["inspection", "confirmation", "blob"])
def test_recovery_read_failures_raise_sanitized_errors_instead_of_no_match(
    repos: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    preview = repos.preview()
    repos.publish(preview)
    if phase == "blob":
        oid = git(repos.customer, "rev-parse", f"main:{INGRESS}/issuer.yaml")
        original_blob = GitRepository.blob

        def fail_blob(self: GitRepository, entry: TreeEntry, **kwargs: Any) -> str:
            if entry.oid == oid:
                raise CustomerGitOpsError("Git read failed", "repository_unavailable")
            return original_blob(self, entry, **kwargs)

        monkeypatch.setattr(GitRepository, "blob", fail_blob)
    else:
        calls = []
        original_run = gitops_git.subprocess.run

        def fail(args: list[str], **kwargs: Any) -> Any:
            if args[:2] == ["git", "ls-remote"]:
                calls.append(args)
                if len(calls) == (1 if phase == "inspection" else 2):
                    kwargs["stdout"].write(f"transport failure {TOKEN}".encode())
                    return subprocess.CompletedProcess(args, 128)
            return original_run(args, **kwargs)

        monkeypatch.setattr(gitops_git.subprocess, "run", fail)
    with pytest.raises(CustomerGitOpsError) as error:
        recover(repos, preview)
    assert error.value.code == "repository_unavailable"
    assert TOKEN not in str(error.value)


def test_recovery_cannot_be_rebound_to_a_different_customer_url(repos: Repositories) -> None:
    preview = repos.preview()
    with pytest.raises(CustomerGitOpsError) as error:
        recover(repos, preview, repo_url="https://forgejo.example.test/other/customer.git")
    assert error.value.code == "invalid_preview"


@pytest.mark.parametrize(
    "change", ["fingerprint", "path", "bases-url", "revision", "pin-upgrade", "version"]
)
def test_recovery_validates_original_preview_integrity_paths_and_base_metadata(
    repos: Repositories,
    change: str,
) -> None:
    repos.seed()
    preview = repos.preview(adopt=True)
    altered = copy.deepcopy(preview)
    if change == "fingerprint":
        altered["fingerprint"] = "0" * 64
    else:
        if change == "path":
            altered["files"]["../escape"] = "unsafe"
        elif change == "bases-url":
            altered["bases_url"] = "http://insecure.example.test/public/bases.git"
        elif change == "revision":
            altered["bases_revision"] = "main"
        elif change == "pin-upgrade":
            altered["bases_revision"] = "a" * 40
        else:
            altered["version"] += 1
        altered["fingerprint"] = customer_gitops._fingerprint(altered)
    with pytest.raises(CustomerGitOpsError) as error:
        recover(repos, altered)
    assert error.value.code == "invalid_preview"


def test_recovery_validates_operation_id(repos: Repositories) -> None:
    preview = repos.preview()
    with pytest.raises(CustomerGitOpsError) as error:
        recover(repos, preview, operation_id="operation\nInjected-Trailer: bad")
    assert error.value.code == "invalid_operation"
