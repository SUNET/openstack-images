"""Isolated Git plumbing; customer trees are never checked out or executed.

Transport credentials exist only in a child process's environment. Public base
fetches use a separate credential-free environment, even on the same HTTPS host.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.gitops_render import BASE_PATH, validate_url
from app.gitops_types import CustomerGitOpsError

GIT_TIMEOUT = 120
ACCESS_CHECK_TIMEOUT = 10
MAX_OUTPUT = 32 * 1024 * 1024
MAX_MANAGED_BLOB = 256 * 1024
SHA_PATTERN = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"


def validate_revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(SHA_PATTERN, value) or set(value) == {"0"}:
        raise CustomerGitOpsError(
            "The bases revision must be a full immutable 40- or 64-character commit SHA",
            "invalid_bases_revision",
        )
    return value


@dataclass(frozen=True)
class Transport:
    url: str
    protocol: str = "https"


def _transport(url: str) -> Transport:
    """The only transport injection seam; tests map HTTPS URLs to disposable local repos."""
    return Transport(validate_url(url))


def isolated_environment() -> dict[str, str]:
    """Exclude ambient Git config, credentials, tracing, askpass and application secrets."""
    env = {
        "PATH": os.defpath if "PATH" not in os.environ else os.environ["PATH"],
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
    }
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def git_environment(
    transport: Transport,
    *,
    repo_url: str | None = None,
    username: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    env = isolated_environment()
    config = [
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("core.attributesFile", os.devnull),
        ("credential.helper", ""),
        ("credential.interactive", "false"),
        ("http.extraHeader", ""),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("protocol.allow", "never"),
        (f"protocol.{transport.protocol}.allow", "always"),
        ("fetch.recurseSubmodules", "false"),
        ("submodule.recurse", "false"),
        ("fetch.fsckObjects", "true"),
        ("transfer.fsckObjects", "true"),
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
    ]
    if repo_url is not None:
        validate_url(repo_url)
        if (
            not username
            or not token
            or ":" in username
            or any(c in username + token for c in "\r\n\x00")
        ):
            raise CustomerGitOpsError(
                "Repository credentials are not configured", "invalid_credentials"
            )
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
        config.append((f"http.{repo_url}.extraHeader", f"Authorization: Basic {credentials}"))
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


def _run(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    data: bytes | None = None,
    check: bool = True,
    code: str = "repository_unavailable",
    timeout: int = GIT_TIMEOUT,
) -> CommandResult:
    # Never retain stderr: transport failures may echo HTTP headers or passwords.
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            output.seek(0)
            stdout = output.read(MAX_OUTPUT + 1)
    except (OSError, subprocess.TimeoutExpired):
        raise CustomerGitOpsError("Git operation failed or timed out", code) from None
    if len(stdout) > MAX_OUTPUT:
        raise CustomerGitOpsError("Repository exceeds publisher limits", "repository_too_large")
    if check and process.returncode:
        raise CustomerGitOpsError("Git operation failed; verify repository access and state", code)
    return CommandResult(process.returncode, stdout)


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    oid: str


def remote_refs(
    transport: Transport, env: dict[str, str], cwd: Path, *, timeout: int = GIT_TIMEOUT
) -> dict[str, str]:
    result = _run(
        ["ls-remote", "--refs", "--", transport.url], env=env, cwd=cwd, timeout=timeout
    )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        try:
            oid, name = line.decode("utf-8").split("\t")
        except (UnicodeError, ValueError):
            raise CustomerGitOpsError(
                "Invalid repository ref advertisement", "unsafe_repository"
            ) from None
        if not re.fullmatch(SHA_PATTERN, oid) or not name.startswith("refs/") or name in refs:
            raise CustomerGitOpsError("Invalid repository ref advertisement", "unsafe_repository")
        refs[name] = oid
    return refs


def check_repository_read_access(repo_url: str, username: str, token: str) -> None:
    """Check supplied Git credentials without cloning, fetching or writing the remote.

    An empty private repository is valid. The caller checks the allowed origin
    and repository privacy through the Forgejo API before invoking this check.
    """
    transport = _transport(repo_url)
    env = git_environment(transport, repo_url=repo_url, username=username, token=token)
    with tempfile.TemporaryDirectory(prefix="customer-repository-access-") as directory:
        remote_refs(transport, env, Path(directory), timeout=ACCESS_CHECK_TIMEOUT)


def main_head(refs: dict[str, str]) -> str | None:
    if refs and "refs/heads/main" not in refs:
        raise CustomerGitOpsError(
            "Repository has refs but no main branch; select or create main before publishing",
            "branch_missing",
        )
    return refs.get("refs/heads/main")


class GitRepository:
    """A disposable bare repository and its explicit, non-recursive transport."""

    def __init__(self, path: Path, transport: Transport, env: dict[str, str]) -> None:
        self.path = path
        self.transport = transport
        self.env = env

    @classmethod
    def customer(
        cls,
        root: Path,
        repo_url: str,
        username: str,
        token: str,
    ) -> tuple[GitRepository, str | None]:
        transport = _transport(repo_url)
        env = git_environment(transport, repo_url=repo_url, username=username, token=token)
        expected = main_head(remote_refs(transport, env, root))
        path = root / "customer.git"
        args = [
            "clone",
            "--bare",
            "--no-local",
            "--no-hardlinks",
            "--no-recurse-submodules",
            "--template=",
        ]
        if expected is not None:
            args.extend(["--single-branch", "--branch=main", "--no-tags"])
        _run([*args, "--", transport.url, str(path)], env=env, cwd=root)
        repo = cls(path, transport, env)
        actual = repo.local_head()
        if actual != expected:
            raise CustomerGitOpsError(
                "Repository changed while being inspected; preview again", "stale_preview"
            )
        return repo, actual

    @classmethod
    def public_base(cls, root: Path, url: str, revision: str) -> GitRepository:
        validate_revision(revision)
        transport = _transport(url)
        env = git_environment(transport)
        path = root / "bases.git"
        algorithm = "sha1" if len(revision) == 40 else "sha256"
        _run(
            ["init", "--bare", "--template=", f"--object-format={algorithm}", str(path)],
            env=env,
            cwd=root,
        )
        repo = cls(path, transport, env)
        repo.run(
            [
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                transport.url,
                revision,
            ],
            code="bases_unavailable",
        )
        actual = (
            repo.run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], code="bases_unavailable")
            .stdout.decode()
            .strip()
        )
        if actual != revision:
            raise CustomerGitOpsError(
                "Bases pin does not identify an immutable commit", "invalid_bases_revision"
            )
        return repo

    def run(
        self,
        args: list[str],
        *,
        data: bytes | None = None,
        check: bool = True,
        code: str = "repository_unavailable",
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        return _run(
            args,
            env=self.env if env is None else env,
            cwd=self.path,
            data=data,
            check=check,
            code=code,
        )

    def local_head(self) -> str | None:
        result = self.run(["show-ref", "--verify", "--hash", "refs/heads/main"], check=False)
        if result.returncode:
            return None
        return result.stdout.decode("ascii").strip()

    def remote_head(self) -> str | None:
        return main_head(remote_refs(self.transport, self.env, self.path))

    def refresh(self) -> str:
        self.run(
            [
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                self.transport.url,
                "+refs/heads/main:refs/heads/main",
            ]
        )
        head = self.local_head()
        if head is None:
            raise CustomerGitOpsError(
                "The main branch disappeared; preview again", "stale_preview"
            )
        return head

    def tree(self, head: str | None) -> dict[str, TreeEntry]:
        if head is None:
            return {}
        entries = {}
        for record in self.run(["ls-tree", "-rzt", "--full-tree", head]).stdout.split(b"\0"):
            if not record:
                continue
            description, path = record.split(b"\t", 1)
            mode, kind, oid = description.decode("ascii").split()
            entries[path.decode("utf-8", errors="surrogateescape")] = TreeEntry(mode, kind, oid)
        return entries

    def blob(self, entry: TreeEntry, *, maximum: int = MAX_MANAGED_BLOB) -> str:
        size = int(self.run(["cat-file", "-s", entry.oid]).stdout)
        if size > maximum:
            raise CustomerGitOpsError(
                "Managed file exceeds publisher limits", "repository_too_large"
            )
        try:
            return self.run(["cat-file", "blob", entry.oid]).stdout.decode("utf-8")
        except UnicodeError:
            raise CustomerGitOpsError(
                "Managed files must be UTF-8 text", "unsafe_repository"
            ) from None

    def validate_gitmodules(self, entry: TreeEntry, bases_url: str) -> None:
        self.blob(entry, maximum=16384)
        raw = self.run(
            ["config", "--no-includes", "--blob", entry.oid, "--null", "--list"],
            code="unsafe_repository",
        ).stdout
        try:
            pairs = [
                tuple(item.decode("utf-8").split("\n", 1)) for item in raw.split(b"\0") if item
            ]
        except UnicodeError:
            raise CustomerGitOpsError(
                "Unsafe bases submodule configuration", "unsafe_repository"
            ) from None
        expected = {
            (f"submodule.{BASE_PATH}.path", BASE_PATH),
            (f"submodule.{BASE_PATH}.url", bases_url),
        }
        if len(pairs) != 2 or set(pairs) != expected:
            raise CustomerGitOpsError(
                "Existing .gitmodules must contain only the configured public bases URL and path",
                "unsafe_repository",
            )

    def commit(
        self,
        *,
        head: str | None,
        files: dict[str, str],
        bases_revision: str,
        operation_id: str,
        fingerprint: str,
        author_name: str,
        author_email: str,
    ) -> str:
        algorithm = self.run(["rev-parse", "--show-object-format"]).stdout.strip()
        if len(bases_revision) != (64 if algorithm == b"sha256" else 40):
            raise CustomerGitOpsError(
                "Customer and bases Git object formats differ", "invalid_bases_revision"
            )
        self.run(["read-tree", head] if head else ["read-tree", "--empty"])
        for path, content in sorted(files.items()):
            oid = (
                self.run(["hash-object", "-w", "--stdin"], data=content.encode())
                .stdout.decode()
                .strip()
            )
            self.run(["update-index", "--add", "--cacheinfo", f"100644,{oid},{path}"])
        self.run(["update-index", "--add", "--cacheinfo", f"160000,{bases_revision},{BASE_PATH}"])
        tree = self.run(["write-tree"]).stdout.decode().strip()
        env = {
            **self.env,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        message = (
            "Publish customer cluster GitOps state\n\n"
            f"Customer-GitOps-Operation: {operation_id}\nCustomer-GitOps-Preview: {fingerprint}\n"
        )
        args = ["commit-tree", tree]
        if head:
            args.extend(["-p", head])
        return self.run(args, data=message.encode(), env=env).stdout.decode().strip()

    def push(self, commit: str, expected_head: str | None) -> bool:
        self._check_commit_parent(commit, expected_head)
        if self.remote_head() != expected_head:
            raise CustomerGitOpsError(
                "Repository changed before push; prepare a fresh preview", "stale_preview"
            )
        # Never force a ref update. Git's fast-forward check and atomic remote
        # old-OID check also protect against writers racing this freshness read.
        result = self.run(
            [
                "push",
                "--porcelain",
                "--recurse-submodules=no",
                "--",
                self.transport.url,
                f"{commit}:refs/heads/main",
            ],
            check=False,
            code="push_failed",
        )
        return result.returncode == 0

    def _check_commit_parent(self, commit: str, expected_head: str | None) -> None:
        """Send only an initial root or an exact single-parent child of the preview HEAD."""
        if (
            not isinstance(commit, str)
            or not re.fullmatch(SHA_PATTERN, commit)
            or (expected_head is not None and not re.fullmatch(SHA_PATTERN, expected_head))
        ):
            raise CustomerGitOpsError("Invalid publication commit", "invalid_commit")
        kind = self.run(["cat-file", "-t", commit], code="invalid_commit").stdout.strip()
        if kind != b"commit":
            raise CustomerGitOpsError("Publication object must be a commit", "invalid_commit")
        headers = self.run(["cat-file", "commit", commit], code="invalid_commit").stdout
        parents = [
            line.removeprefix(b"parent ")
            for line in headers.partition(b"\n\n")[0].splitlines()
            if line.startswith(b"parent ")
        ]
        expected = [expected_head.encode("ascii")] if expected_head is not None else []
        if parents != expected:
            raise CustomerGitOpsError(
                "Publication commit must have exactly the preview HEAD as its parent",
                "invalid_commit",
            )


def check_managed_paths(entries: dict[str, TreeEntry], paths: set[str]) -> None:
    for path in paths | {BASE_PATH, ".gitmodules", "README.md"}:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            ancestor = entries.get("/".join(parts[:index]))
            if ancestor and ancestor.mode != "040000":
                raise CustomerGitOpsError(
                    "A generated path has an unsafe ancestor", "unsafe_repository"
                )
        entry = entries.get(path)
        mode = "160000" if path == BASE_PATH else "100644"
        if entry and entry.mode != mode:
            raise CustomerGitOpsError(
                "A generated path is a symlink, gitlink, directory or executable",
                "unsafe_repository",
            )
    if any(entry.mode == "160000" and path != BASE_PATH for path, entry in entries.items()):
        raise CustomerGitOpsError("Unrecognized repository gitlink", "unsafe_repository")


def existing_bases(
    repo: GitRepository, entries: dict[str, TreeEntry], bases_url: str
) -> str | None:
    modules, link = entries.get(".gitmodules"), entries.get(BASE_PATH)
    if bool(modules) != bool(link):
        raise CustomerGitOpsError(
            "Bases .gitmodules and gitlink must exist together", "unsafe_repository"
        )
    if modules and link:
        repo.validate_gitmodules(modules, bases_url)
        return validate_revision(link.oid)
    return None
