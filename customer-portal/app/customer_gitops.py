"""Preview and publish the customer-owned GitOps tree using optimistic concurrency.

The caller validates the allowed customer HTTPS origin, serializes operations per
repository, and persists the complete preview before invoking publish_preview.
Persist preview["files"] as the next baseline only after a confirmed publication.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings
from app.gitops_git import (
    SHA_PATTERN,
    GitRepository,
    TreeEntry,
    check_managed_paths,
    existing_bases,
    validate_revision,
)
from app.gitops_render import BASE_PATH, render_tree, validate_url
from app.gitops_types import CustomerGitOpsError, Preview
from app.gitops_validate import (
    canonical_tree,
    documents,
    known_paths,
    semantic,
    validate_kustomize,
    validate_paths,
    validate_tree,
)

__all__ = [
    "CustomerGitOpsError",
    "render_tree",
    "validate_tree",
    "prepare_preview",
    "recover_preview",
    "publish_preview",
]
PREVIEW_VERSION = 1


def _semantic(content: str) -> str:
    return semantic(content)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fingerprint(preview: dict[str, Any]) -> str:
    return _digest(
        json.dumps(
            {key: value for key, value in preview.items() if key != "fingerprint"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )


def _read_manifests(
    repo: GitRepository,
    entries: dict[str, TreeEntry],
    paths: set[str],
) -> dict[str, str]:
    return {
        path: repo.blob(entries[path])
        for path in paths
        if path.endswith(".yaml") and path in entries
    }


def _merge(
    *,
    proposed: dict[str, str],
    current: dict[str, str],
    baseline: dict[str, str],
    adopt: bool,
) -> tuple[dict[str, str], bool]:
    merged = {}
    adopting = False
    for path, desired in proposed.items():
        if not path.endswith(".yaml"):
            continue
        old, now = baseline.get(path), current.get(path)
        if old is None and now is not None:
            if not adopt:
                raise CustomerGitOpsError(
                    "Existing generated manifests require explicit adoption",
                    "adoption_required",
                )
            if _semantic(now) != _semantic(desired):
                raise CustomerGitOpsError(
                    "Existing manifests are incompatible with this customer, cluster "
                    "or requested settings",
                    "incompatible_adoption",
                )
            adopting = True
            merged[path] = desired
        elif now is None:
            if old is not None:
                raise CustomerGitOpsError(
                    "A managed manifest was manually deleted", "manual_conflict"
                )
            merged[path] = desired
        elif _semantic(now) == _semantic(desired) or _semantic(now) == _semantic(old):
            merged[path] = desired
        elif _semantic(desired) == _semantic(old):
            # Preserve a disjoint, compatible manual change. The complete result
            # must still match the reviewed schema and all cross-file identities.
            merged[path] = now
        else:
            raise CustomerGitOpsError(
                "A managed manifest has overlapping manual and proposed changes",
                "manual_conflict",
            )
    return merged, adopting


def _diff(
    current: dict[str, str],
    approved: dict[str, str],
    *,
    redact_previous: bool = False,
) -> tuple[str, list[str]]:
    """Diff only validated manifest objects; never include comments or customer docs."""
    from app.gitops_render import dump_documents

    chunks = []
    formatting = []
    for path in sorted(approved):
        if not path.endswith(".yaml"):
            continue
        old, new = current.get(path), approved[path]
        if old is not None and _semantic(old) == _semantic(new):
            if old != new:
                formatting.append(path)
            continue
        old_text = dump_documents(documents(old)) if old is not None else ""
        if old is not None and redact_previous:
            old_text = "# Previous managed schema: existing contents withheld.\n"
        new_text = dump_documents(documents(new))
        chunks.extend(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}",
            )
        )
    return "".join(chunks), formatting


def _observed(entries: dict[str, TreeEntry], paths: set[str]) -> dict[str, str | None]:
    return {path: entries[path].oid if path in entries else None for path in sorted(paths)}


def _configured_url(settings: Settings) -> str:
    return validate_url(settings.customer_cluster_bases_url)


def prepare_preview(
    *,
    repo_url: str,
    username: str,
    token: str,
    files: dict[str, str],
    baseline: dict[str, str],
    settings: Settings,
    adopt: bool = False,
) -> dict[str, Any]:
    """Inspect refs, merge managed files, validate builds, and return a durable preview.

    Conflicts are file-granular (YAML formatting is ignored). Compatible manual
    ACME-contact edits may be retained in an otherwise unchanged file. Cluster
    identity, ingress VIP and interface must match the operator's proposal.
    Two divergent changes to one file need a fresh resolution.
    Existing READMEs and a validated .gitmodules are preserved and omitted from
    managed contents. Their arbitrary contents never enter the preview or diff.
    """
    validate_url(repo_url)
    bases_url = _configured_url(settings)
    proposed = canonical_tree(files, repo_url, bases_url)
    proposed_inputs = validate_tree(proposed, repo_url, bases_url)
    slug = proposed_inputs.slug
    if not isinstance(baseline, dict):
        raise CustomerGitOpsError("Invalid managed baseline", "invalid_baseline")
    if baseline:
        validate_paths(baseline, slug, complete=False)
    paths = known_paths(slug)
    with tempfile.TemporaryDirectory(prefix="customer-gitops-") as temporary:
        root = Path(temporary)
        repo, head = GitRepository.customer(root, repo_url, username, token)
        entries = repo.tree(head)
        check_managed_paths(entries, paths)
        prior_pin = existing_bases(repo, entries, bases_url)
        revision = prior_pin or validate_revision(settings.customer_cluster_bases_revision)
        current = _read_manifests(repo, entries, paths)
        approved, adopting = _merge(
            proposed=proposed, current=current, baseline=baseline, adopt=adopt
        )
        # Old portal-owned schemas may legitimately need upgrading. Withhold
        # their previous contents from the diff instead of trusting arbitrary
        # fields. The three-way merge still rejects divergent manual edits.
        redact_previous = False
        if current:
            inspected = {**proposed, **current}
            for path in list(inspected):
                if not path.endswith(".yaml"):
                    del inspected[path]
            try:
                validate_tree(inspected, repo_url, bases_url)
            except CustomerGitOpsError:
                redact_previous = True
        try:
            approved = canonical_tree(approved, repo_url, bases_url)
            approved_inputs = validate_tree(approved, repo_url, bases_url)
        except CustomerGitOpsError:
            raise CustomerGitOpsError(
                "Managed manifests contain incompatible or inconsistent manual changes",
                "manual_conflict",
            ) from None
        if (
            approved_inputs.slug,
            approved_inputs.hostname,
            approved_inputs.ingress_vip,
            approved_inputs.interface,
        ) != (
            proposed_inputs.slug,
            proposed_inputs.hostname,
            proposed_inputs.ingress_vip,
            proposed_inputs.interface,
        ):
            raise CustomerGitOpsError(
                "Manual edits conflict with the operator-provided cluster identity "
                "or ingress settings",
                "manual_conflict",
            )
        for path, content in proposed.items():
            if not path.endswith(".yaml") and path not in entries:
                approved[path] = content
        diff, formatting = _diff(current, approved, redact_previous=redact_previous)
        changed = (
            any(
                path not in entries or (path.endswith(".yaml") and current[path] != content)
                for path, content in approved.items()
            )
            or prior_pin is None
        )
        if head is None:
            action = "initialize"
        elif adopting:
            action = "adopt"
        elif not changed:
            action = "noop"
        elif not current:
            action = "add"
        else:
            action = "update"
        validation = validate_kustomize(root, approved, bases_url, revision)
        if repo.remote_head() != head:
            raise CustomerGitOpsError(
                "Repository changed during validation; preview again", "stale_preview"
            )
        preview: Preview = {
            "version": PREVIEW_VERSION,
            "repo_url": repo_url,
            "slug": slug,
            "expected_head": head,
            "files": approved,
            "observed": _observed(entries, set(approved) | {".gitmodules"}),
            "observed_bases_revision": prior_pin,
            "bases_revision": revision,
            "bases_url": bases_url,
            "diff": diff,
            "diff_kind": "semantic-manifests",
            "formatting_paths": formatting,
            "action": action,
            "validation": validation,
            "fingerprint": "",
        }
        preview["fingerprint"] = _fingerprint(preview)
        return dict(preview)


def _validate_preview(
    preview: dict[str, Any],
    repo_url: str,
    settings: Settings | None = None,
) -> None:
    try:
        if (
            preview["version"] != PREVIEW_VERSION
            or preview["repo_url"] != repo_url
            or preview["fingerprint"] != _fingerprint(preview)
        ):
            raise ValueError()
        validate_url(preview["bases_url"])
        if settings is not None and preview["bases_url"] != _configured_url(settings):
            raise ValueError()
        slug = validate_paths(preview["files"])
        if slug != preview["slug"]:
            raise ValueError()
        validate_tree(preview["files"], repo_url, preview["bases_url"])
        validate_revision(preview["bases_revision"])
        for head in (preview["expected_head"], preview["observed_bases_revision"]):
            if head is not None and not re.fullmatch(SHA_PATTERN, head):
                raise ValueError()
        if preview["observed_bases_revision"] not in (None, preview["bases_revision"]):
            raise ValueError()
        observed = preview["observed"]
        if not isinstance(observed, dict) or set(observed) != set(preview["files"]) | {
            ".gitmodules"
        }:
            raise ValueError()
        if any(
            oid is not None and not re.fullmatch(SHA_PATTERN, oid) for oid in observed.values()
        ):
            raise ValueError()
        if preview["action"] not in {"initialize", "add", "update", "adopt", "noop"}:
            raise ValueError()
    except (KeyError, TypeError, ValueError, RecursionError):
        raise CustomerGitOpsError(
            "Invalid or altered preview; prepare a fresh preview", "invalid_preview"
        ) from None


def _matches(
    repo: GitRepository,
    entries: dict[str, TreeEntry],
    preview: dict[str, Any],
    *,
    exact: bool = False,
) -> bool:
    if existing_bases(repo, entries, preview["bases_url"]) != preview["bases_revision"]:
        return False
    for path, desired in preview["files"].items():
        if not exact and path.endswith("README.md"):
            continue
        if path not in entries:
            return False
        # Existing inert documentation is never overwritten, including a retry
        # after a successful push followed by a customer's README edit.
        if path.endswith("README.md") or path == ".gitmodules":
            continue
        try:
            current = repo.blob(entries[path])
            matches = current == desired if exact else _semantic(current) == _semantic(desired)
            if not matches:
                return False
        except CustomerGitOpsError as error:
            if error.code == "invalid_manifests":
                return False
            raise
    return True


def _conflict(entries: dict[str, TreeEntry], preview: dict[str, Any]) -> None:
    changed = any(
        (entries[path].oid if path in entries else None) != oid
        for path, oid in preview["observed"].items()
        if not path.endswith("README.md")
    )
    pin = entries.get(BASE_PATH)
    changed = changed or (pin.oid if pin else None) != preview["observed_bases_revision"]
    if changed:
        raise CustomerGitOpsError(
            "Managed paths changed after preview; resolve conflicts and preview again",
            "manual_conflict",
        )
    raise CustomerGitOpsError(
        "Repository HEAD changed after preview; prepare a fresh preview", "stale_preview"
    )


def _confirm(repo: GitRepository, preview: dict[str, Any], candidate: str | None = None) -> str:
    for _ in range(3):
        remote = repo.remote_head()
        if remote is None:
            raise CustomerGitOpsError(
                "Publication is not confirmed on main; retry the operation", "push_unconfirmed"
            )
        if candidate is not None and remote == candidate:
            return candidate
        head = repo.refresh()
        entries = repo.tree(head)
        check_managed_paths(entries, known_paths(preview["slug"]))
        if not _matches(repo, entries, preview):
            raise CustomerGitOpsError(
                "Approved contents are not confirmed on main; retry or preview again",
                "push_unconfirmed",
            )
        if repo.remote_head() == head:
            return head
    raise CustomerGitOpsError(
        "Repository keeps changing; retry publication confirmation", "push_unconfirmed"
    )


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", operation_id
    ):
        raise CustomerGitOpsError("Invalid publication operation ID", "invalid_operation")


def recover_preview(
    *,
    repo_url: str,
    username: str,
    token: str,
    preview: dict[str, Any],
    operation_id: str,
    settings: Settings,
) -> str | None:
    """Read back an approved state using only its immutable, persisted metadata.

    Deployment defaults may change independently of an already published tree;
    settings are accepted for API consistency but are not consulted for recovery.
    Matching manifests and the original safe gitlink are sufficient, including
    an approved no-op with no operation marker. Inert READMEs are preserved.

    Return the confirmed current main SHA, or None when it does not match.
    Invalid previews and transport failures remain sanitized exceptions. This
    path performs customer Git reads only: no base fetch, build or readiness gate.
    """
    validate_url(repo_url)
    _validate_preview(preview, repo_url)
    _validate_operation_id(operation_id)
    with tempfile.TemporaryDirectory(prefix="customer-gitops-recover-") as temporary:
        try:
            repo, head = GitRepository.customer(Path(temporary), repo_url, username, token)
        except CustomerGitOpsError as error:
            if error.code in {"branch_missing", "stale_preview"}:
                return None
            raise
        if head is None:
            return None
        try:
            entries = repo.tree(head)
            check_managed_paths(entries, known_paths(preview["slug"]))
            if not _matches(repo, entries, preview):
                return None
            return _confirm(repo, preview, head)
        except CustomerGitOpsError as error:
            if error.code in {
                "unsafe_repository",
                "invalid_manifests",
                "repository_too_large",
                "branch_missing",
                "stale_preview",
                "push_unconfirmed",
            }:
                return None
            raise


def publish_preview(
    *,
    repo_url: str,
    username: str,
    token: str,
    preview: dict[str, Any],
    operation_id: str,
    settings: Settings,
    before_push: Callable[[], None] | None = None,
) -> str:
    """Publish exactly an approved preview and return an independently confirmed SHA.

    Exact retries also succeed when main has advanced without changing approved
    managed state. Push exit status alone is never treated as proof of success.
    Invoke the synchronous before_push gate after builds and commit generation
    on every writing attempt. Its exceptions propagate unchanged. Confirmation
    of an existing approved state does not invoke this write-readiness gate.
    """
    validate_url(repo_url)
    _validate_preview(preview, repo_url, settings)
    _validate_operation_id(operation_id)
    for value in (settings.git_author_name, settings.git_author_email):
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00<>"):
            raise CustomerGitOpsError("Invalid Git author configuration", "invalid_configuration")
    with tempfile.TemporaryDirectory(prefix="customer-gitops-") as temporary:
        root = Path(temporary)
        repo, head = GitRepository.customer(root, repo_url, username, token)
        entries = repo.tree(head)
        check_managed_paths(entries, known_paths(preview["slug"]))
        if head and _matches(repo, entries, preview, exact=head == preview["expected_head"]):
            # Validation is still mandatory on retries; installation failures and
            # unsafe pinned bases must not be reported as successful validation.
            validate_kustomize(
                root, preview["files"], preview["bases_url"], preview["bases_revision"]
            )
            return _confirm(repo, preview, head)
        if head != preview["expected_head"]:
            _conflict(entries, preview)
        if _observed(entries, set(preview["observed"])) != preview["observed"]:
            _conflict(entries, preview)
        prior_pin = existing_bases(repo, entries, preview["bases_url"])
        if prior_pin != preview["observed_bases_revision"]:
            _conflict(entries, preview)
        validate_kustomize(root, preview["files"], preview["bases_url"], preview["bases_revision"])
        files = {
            path: content
            for path, content in preview["files"].items()
            if not (path in entries and (path.endswith("README.md") or path == ".gitmodules"))
        }
        commit = repo.commit(
            head=head,
            files=files,
            bases_revision=preview["bases_revision"],
            operation_id=operation_id,
            fingerprint=preview["fingerprint"],
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
        )
        if before_push is not None:
            before_push()
        # Even an exception/timeout may mean the server accepted the push but its
        # response was lost. Read back main before deciding whether to retry.
        try:
            pushed = repo.push(commit, head)
        except CustomerGitOpsError:
            pushed = False
        try:
            return _confirm(repo, preview, commit)
        except CustomerGitOpsError as error:
            code = "push_unconfirmed" if pushed else "push_failed"
            raise CustomerGitOpsError(
                "Publication could not be confirmed on main; retry the operation", code
            ) from error


def publish_tree(
    *,
    repo_url: str,
    username: str,
    token: str,
    files: dict[str, str],
    settings: Settings,
) -> str:
    """Compatibility entry point while callers migrate to persisted preview operations."""
    preview = prepare_preview(
        repo_url=repo_url,
        username=username,
        token=token,
        files=files,
        baseline={},
        settings=settings,
    )
    return publish_preview(
        repo_url=repo_url,
        username=username,
        token=token,
        preview=preview,
        operation_id=preview["fingerprint"],
        settings=settings,
    )
