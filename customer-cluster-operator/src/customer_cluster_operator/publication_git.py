"""Isolated Git transport and index-only snapshots for inventory publication."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .errors import InventoryConflict, ValidationError


class GitError(RuntimeError):
    """A Git failure containing no child output, credentials, or remote text."""


def validate_git_config(config: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(config, dict):
        raise ValidationError("Git publication configuration is required")
    url, branch, username = (config.get(key) for key in ("repoUrl", "branch", "username"))
    try:
        if not isinstance(url, str) or not re.fullmatch(r"[A-Za-z0-9:/._~+\[\]-]+", url):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (not parsed.port and parsed.netloc.endswith(":"))
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or "//" in parsed.path
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError(
            "Git repository must be a safe HTTPS URL without credentials"
        ) from None
    if (
        not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
        or ".." in branch
        or any(not part or part.startswith(".") or part.endswith((".lock", "."))
               for part in branch.split("/"))
    ):
        raise ValidationError("Git branch is invalid")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._@+-]+", username):
        raise ValidationError("Git username is invalid")
    return url, branch, username


def _git_env(repo_url: str, username: str, token: str) -> dict[str, str]:
    if not isinstance(token, str) or not token:
        raise ValidationError("Git token is empty")
    if any(character in token for character in "\x00\n\r"):
        raise ValidationError("Git token contains invalid characters")
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    settings = {
        f"http.{repo_url}.extraHeader": f"Authorization: Basic {credentials}",
        "http.followRedirects": "false",
        "credential.helper": "",
        "credential.interactive": "false",
        "core.hooksPath": os.devnull,
        "core.attributesFile": os.devnull,
        "core.fsmonitor": "false",
        "core.autocrlf": "false",
        "init.templateDir": "",
        "submodule.recurse": "false",
        "fetch.recurseSubmodules": "false",
        "push.recurseSubmodules": "no",
        "commit.gpgSign": "false",
        "gc.auto": "0",
        "maintenance.auto": "false",
    }
    env = {
        "PATH": os.defpath,
        "HOME": os.devnull,
        "XDG_CONFIG_HOME": os.devnull,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_COUNT": str(len(settings)),
        "GIT_AUTHOR_NAME": "customer-cluster-operator",
        "GIT_AUTHOR_EMAIL": "customer-cluster-operator@sunet.se",
        "GIT_COMMITTER_NAME": "customer-cluster-operator",
        "GIT_COMMITTER_EMAIL": "customer-cluster-operator@sunet.se",
    }
    for index, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _run_git(
    args: list[str], cwd: Path | None, env: dict[str, str], *, input: bytes | None = None,
) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, input=input,
            check=True, capture_output=True, timeout=300,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # Git can echo credentials from helpers, servers, or transport failures.
        raise GitError("Git publication command failed") from None


def _transport(repo_url: str) -> tuple[str, str]:
    """Production is HTTPS-only; tests explicitly replace this with a local bare repository."""
    return repo_url, "https"


def _oid(output: bytes) -> str:
    if not re.fullmatch(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})\n?", output):
        raise GitError("Git returned an invalid object identity")
    return output.decode("ascii").strip()


@dataclass
class Snapshot:
    repo: Path
    env: dict[str, str] = field(repr=False)
    branch: str
    head: str

    def run(self, *args: str, input: bytes | None = None) -> bytes:
        return _run_git(list(args), self.repo, self.env, input=input)

    def read(self, relative: str) -> bytes | None:
        """Inspect every tree ancestor; never follow a symlink or a gitlink."""
        path = PurePosixPath(relative)
        for part in [*reversed(path.parents), path]:
            if str(part) == ".":
                continue
            entry = self.run("ls-tree", "-z", self.head, "--", str(part))
            if not entry:
                return None
            try:
                metadata, name = entry.removesuffix(b"\0").split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ")
                if name != str(part).encode():
                    raise ValueError
                identity = _oid(oid)
            except ValueError:
                raise GitError("Git returned an invalid tree entry") from None
            expected = (b"100644", b"blob") if part == path else (b"040000", b"tree")
            if (mode, kind) != expected:
                raise InventoryConflict("Publication path has an unsafe Git mode or ancestor")
        return self.run("cat-file", "blob", identity)

    def remote_head(self) -> str:
        ref = f"refs/heads/{self.branch}"
        output = self.run("ls-remote", "--refs", "--exit-code", "origin", ref)
        try:
            identity, actual_ref = output.rstrip(b"\n").split(b"\t")
            if actual_ref != ref.encode():
                raise ValueError
        except ValueError:
            raise GitError("Git remote branch could not be confirmed") from None
        return _oid(identity)

    def commit(self, changes: dict[str, bytes], slug: str) -> None:
        self.run("read-tree", self.head)
        for path, content in changes.items():
            oid = _oid(self.run("hash-object", "-w", "--stdin", input=content))
            self.run("update-index", "--add", "--cacheinfo", f"100644,{oid},{path}")
        self.run(
            "commit", "--quiet", "--no-gpg-sign", "-m",
            f"Generate cluster inventories for {slug}",
        )

    def push(self) -> None:
        self.run("push", "--quiet", "--no-recurse-submodules", "origin",
                 f"HEAD:refs/heads/{self.branch}")


@contextmanager
def clone(url: str, branch: str, env: dict[str, str]) -> Iterator[Snapshot]:
    transport_url, protocol = _transport(url)
    child_env = {**env, "GIT_ALLOW_PROTOCOL": protocol}
    with tempfile.TemporaryDirectory(prefix="cluster-inventory-") as temp:
        repo = Path(temp) / "repo"
        _run_git([
            "clone", "--quiet", "--no-checkout", "--no-local", "--no-recurse-submodules",
            "--single-branch", "--branch", branch, "--template=", "--", transport_url, str(repo),
        ], Path(temp), child_env)
        head = _oid(_run_git([
            "rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}",
        ], repo, child_env))
        yield Snapshot(repo, child_env, branch, head)
