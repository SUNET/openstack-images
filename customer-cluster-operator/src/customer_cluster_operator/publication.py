"""Atomic, ownership-checked publication of hosts and cluster policy."""

from __future__ import annotations

import time
from typing import Any

from .errors import ValidationError
from .publication_git import GitError, Snapshot, _git_env, clone, validate_git_config
from .publication_policy import PolicyInputs, policy_content, validate_hosts, validate_source


def _read_targets(
    snapshot: Snapshot, data: dict[str, Any], policy: PolicyInputs, paths: tuple[str, str],
) -> tuple[bytes | None, bytes | None]:
    validate_source(snapshot.read(f"clusters/{policy.slug}/cluster.yaml"), data, policy)
    hosts, cluster_policy = (snapshot.read(path) for path in paths)
    if hosts is not None:
        validate_hosts(hosts)
    policy_content(cluster_policy, policy)
    return hosts, cluster_policy


def publish_inventory(
    git_config: dict[str, Any], slug: str, inventory: str, token: str, retries: int = 3,
    *, cluster_policy: str, provisioning_data: dict[str, Any],
) -> tuple[str, str]:
    """Publish both artifacts in one commit, returning only a confirmed remote commit."""
    policy = PolicyInputs.from_data(provisioning_data)
    if slug != policy.slug:
        raise ValidationError("Publication cluster name does not match the job")
    if cluster_policy != policy.render():
        raise ValidationError("Publication policy does not match the validated job")
    if not isinstance(inventory, str):
        raise ValidationError("Publication hosts inventory must be text")
    hosts_content = inventory.encode()
    validate_hosts(hosts_content)
    if type(retries) is not int or not 1 <= retries <= 10:
        raise ValidationError("Publication retries must be between one and ten")
    url, branch, username = validate_git_config(git_config)
    env = _git_env(url, username, token)
    paths = (
        f"clusters/{slug}/generated/ansible/hosts.yml",
        f"inventory/clusters/{slug}.yml",
    )
    for attempt in range(retries):
        try:
            with clone(url, branch, env) as snapshot:
                old_hosts, old_policy = _read_targets(snapshot, provisioning_data, policy, paths)
                desired = (hosts_content, policy_content(old_policy, policy))
                changes = {
                    path: content
                    for path, content, previous in zip(
                        paths, desired, (old_hosts, old_policy), strict=True,
                    )
                    if content != previous
                }
                if not changes:
                    if snapshot.remote_head() == snapshot.head:
                        return paths[0], snapshot.head
                else:
                    snapshot.commit(changes, slug)
                    try:
                        snapshot.push()
                    except GitError:
                        # A failed response does not tell us whether the server accepted the push.
                        pass
                    with clone(url, branch, env) as confirmation:
                        actual = _read_targets(confirmation, provisioning_data, policy, paths)
                        if actual == desired and confirmation.remote_head() == confirmation.head:
                            return paths[0], confirmation.head
        except (GitError, OSError):
            # Never chain subprocess output into worker logs/status, including on the last retry.
            pass
        if attempt + 1 < retries:
            time.sleep(2**attempt)
    raise RuntimeError("Failed to confirm inventory publication on the remote branch") from None
