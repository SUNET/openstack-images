"""Git backend for managing OpenstackProject YAML files."""

import logging
import re
import threading
from pathlib import Path

import git
import yaml

from app.config import Settings
from app.git_url import git_auth_environment
from app.schemas import QuotaSpec
from app.yaml_utils import dump_yaml

logger = logging.getLogger(__name__)

# Default quotas for self-service projects. Derived from QuotaSpec so the
# CR defaults, the API validation bounds and the UI pre-fill never drift.
DEFAULT_QUOTAS = QuotaSpec().model_dump()
MANAGED_PROJECT_READ_ONLY_DETAIL = (
    "This SUNET-managed project is read-only; changes, moves, or deletion require "
    "the cluster workflow or coordinated decommissioning with SUNET"
)
MANAGED_CONTRACT_RENAME_DETAIL = (
    "This contract contains a SUNET-managed project and is read-only for renaming; "
    "coordinate decommissioning with SUNET before changing its contract number"
)


class ManagedProjectMutationError(ValueError):
    """Raised when a generic mutation targets managed project state."""


def require_project_mutable(project: dict) -> None:
    """Reject mutation of a managed project."""
    if project.get("managed"):
        raise ManagedProjectMutationError(MANAGED_PROJECT_READ_ONLY_DETAIL)


def require_contract_renamable(projects: list[dict]) -> None:
    """Reject contract rename when any linked project is managed."""
    if any(project.get("managed") for project in projects):
        raise ManagedProjectMutationError(MANAGED_CONTRACT_RENAME_DETAIL)


def _sanitize_name(name: str) -> str:
    """Sanitize a project name for use as a K8s resource name and filename."""
    sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized[:63]


def managed_role_bindings(settings: Settings, users: list[str]) -> list[dict]:
    """Build the customer-reader and cluster-provisioner binding contract."""
    return [
        {
            "role": "reader",
            "users": users,
            "userDomain": settings.default_domain,
        },
        {
            "role": "member",
            "users": [settings.cluster_provisioner_user],
            "userDomain": settings.cluster_provisioner_user_domain,
        },
    ]


def _parse_project(doc: dict) -> dict:
    """Extract project info from a parsed YAML document."""
    spec = doc.get("spec", {})
    resource_name = doc.get("metadata", {}).get("name", "")
    role_bindings = spec.get("roleBindings", [])
    managed = bool(spec.get("managed", False))
    users = []
    for rb in role_bindings:
        if managed and rb.get("role") != "reader":
            continue
        users.extend(rb.get("users", []))
    return {
        "resource_name": resource_name,
        "name": spec.get("name", ""),
        "description": spec.get("description", ""),
        "contract_number": spec.get("contractNumber", ""),
        "users": users,
        "quotas": spec.get("quotas") or DEFAULT_QUOTAS,
        "managed": managed,
    }


class GitBackend:
    """Manages OpenstackProject YAML files in a git repository."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_dir = Path(settings.project_git_work_dir)
        self.projects_dir = self.work_dir / "projects"
        self.repo: git.Repo | None = None
        self._lock = threading.Lock()
        self._auth_env = git_auth_environment(
            settings.project_git_repo_url,
            settings.project_git_username,
            settings.project_git_token,
        )

    def init(self) -> None:
        """Clone or pull the repository."""
        if (self.work_dir / ".git").exists():
            self.repo = git.Repo(self.work_dir)
            self.repo.remotes.origin.set_url(self.settings.project_git_repo_url)
            with self.repo.git.custom_environment(**self._auth_env):
                self.repo.remotes.origin.pull(self.settings.project_git_branch)
            logger.info(
                "Pulled latest changes from %s",
                self.settings.project_git_repo_url,
            )
        else:
            self.repo = git.Repo.clone_from(
                self.settings.project_git_repo_url,
                self.work_dir,
                branch=self.settings.project_git_branch,
                env=self._auth_env,
            )
            logger.info(
                "Cloned %s to %s",
                self.settings.project_git_repo_url,
                self.work_dir,
            )

        self.projects_dir.mkdir(exist_ok=True)

    def _pull(self) -> None:
        """Pull latest changes before writing."""
        if self.repo:
            with self.repo.git.custom_environment(**self._auth_env):
                self.repo.remotes.origin.pull(self.settings.project_git_branch)

    def _commit_and_push(self, message: str, max_retries: int = 3) -> None:
        """Stage all changes, commit, and push with retry on conflict."""
        if self.repo is None:
            raise RuntimeError("Git repo not initialized")

        try:
            self.repo.git.add("-A")
            self.repo.index.commit(
                message,
                author=git.Actor(
                    self.settings.git_author_name,
                    self.settings.git_author_email,
                ),
                committer=git.Actor(
                    self.settings.git_author_name,
                    self.settings.git_author_email,
                ),
            )

            for attempt in range(max_retries):
                try:
                    with self.repo.git.custom_environment(**self._auth_env):
                        results = self.repo.remotes.origin.push(self.settings.project_git_branch)
                    failure_flags = (
                        git.remote.PushInfo.ERROR
                        | git.remote.PushInfo.REJECTED
                        | git.remote.PushInfo.REMOTE_REJECTED
                        | git.remote.PushInfo.REMOTE_FAILURE
                    )
                    failures = [result for result in results if result.flags & failure_flags]
                    if failures:
                        summaries = "; ".join(result.summary for result in failures)
                        raise git.GitCommandError("push", 1, stderr=summaries)
                    logger.info("Pushed: %s", message)
                    return
                except git.GitCommandError:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(
                        "Push failed (attempt %d), rebasing and retrying",
                        attempt + 1,
                    )
                    with self.repo.git.custom_environment(**self._auth_env):
                        self.repo.git.pull(
                            "--rebase",
                            "origin",
                            self.settings.project_git_branch,
                        )
        except Exception:
            try:
                self.repo.git.rebase("--abort")
            except git.GitCommandError:
                pass
            self.repo.git.reset(
                "--hard",
                f"origin/{self.settings.project_git_branch}",
            )
            self.repo.git.clean("-fd")
            raise

    def _update_kustomization(self) -> None:
        """Update kustomization.yaml to list all project files."""
        kustomization_path = self.work_dir / "kustomization.yaml"
        project_files = sorted(f"projects/{p.name}" for p in self.projects_dir.glob("*.yaml"))

        kustomization = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": project_files,
        }

        with open(kustomization_path, "w") as f:
            f.write(dump_yaml(kustomization))

    def _render_project_cr(
        self,
        contract_number: str,
        project_name: str,
        description: str,
        users: list[str],
        *,
        managed: bool = False,
        quotas: dict | None = None,
    ) -> dict:
        """Render an OpenstackProject CR as a dict."""
        resource_name = _sanitize_name(project_name)

        role_bindings = [
            {
                "role": "member",
                "users": users,
                "userDomain": self.settings.default_domain,
            }
        ]
        if managed:
            role_bindings = managed_role_bindings(self.settings, users)

        spec: dict = {
            "name": project_name,
            "domain": self.settings.default_domain,
            "description": description,
            "enabled": True,
            "contractNumber": contract_number,
            "quotas": quotas if quotas is not None else DEFAULT_QUOTAS,
            "roleBindings": role_bindings,
            "federationRef": {
                "configMapName": self.settings.federation_config_map,
                "configMapNamespace": self.settings.federation_config_namespace,
            },
        }
        if managed:
            spec["managed"] = True

        return {
            "apiVersion": "sunet.se/v1alpha1",
            "kind": "OpenstackProject",
            "metadata": {"name": resource_name},
            "spec": spec,
        }

    def _read_yaml(self, resource_name: str) -> tuple[Path, dict] | None:
        """Read and parse a project YAML file. Returns (path, doc) or None."""
        file_path = self.projects_dir / f"{resource_name}.yaml"
        if not file_path.exists():
            return None
        with open(file_path) as f:
            doc = yaml.safe_load(f)
        if not doc or doc.get("kind") != "OpenstackProject":
            return None
        return file_path, doc

    def _write_yaml(self, file_path: Path, doc: dict) -> None:
        """Write a YAML document to a file."""
        with open(file_path, "w") as f:
            f.write("---\n")
            f.write(dump_yaml(doc))

    # --- Public API ---

    def get_project(self, resource_name: str) -> dict | None:
        """Get a single project by resource name."""
        self._pull()
        result = self._read_yaml(resource_name)
        if not result:
            return None
        _, doc = result
        return _parse_project(doc)

    def list_projects(self, contract_number: str | None = None) -> list[dict]:
        """List projects from YAML files, optionally filtered by contract number."""
        self._pull()
        projects = []

        for path in self.projects_dir.glob("*.yaml"):
            with open(path) as f:
                doc = yaml.safe_load(f)
            if not doc or doc.get("kind") != "OpenstackProject":
                continue

            proj = _parse_project(doc)
            if contract_number and proj["contract_number"] != contract_number:
                continue
            projects.append(proj)

        return projects

    def write_project(
        self,
        contract_number: str,
        project_name: str,
        description: str,
        users: list[str],
        *,
        managed: bool = False,
        quotas: dict | None = None,
    ) -> str:
        """Create an OpenstackProject YAML file, commit, and push.

        Returns the sanitized resource name. When `managed=True`, the CR is
        marked SUNET-managed: the operator assigns customer-domain users the
        Keystone `reader` role rather than `member`, and portal-side mutation
        endpoints reject non-admin write attempts.

        `quotas` overrides the per-resource quotas; `None` uses DEFAULT_QUOTAS.
        """
        with self._lock:
            self._pull()

            resource_name = _sanitize_name(project_name)
            file_path = self.projects_dir / f"{resource_name}.yaml"

            if file_path.exists():
                raise ValueError(f"Project '{resource_name}' already exists")

            cr = self._render_project_cr(
                contract_number,
                project_name,
                description,
                users,
                managed=managed,
                quotas=quotas,
            )
            self._write_yaml(file_path, cr)
            self._update_kustomization()
            kind = "managed project" if managed else "project"
            self._commit_and_push(f"Add {kind} {project_name} (contract {contract_number})")

            return resource_name

    def update_project(
        self,
        resource_name: str,
        description: str | None = None,
        users: list[str] | None = None,
        *,
        role_bindings: list[dict] | None = None,
        quotas: dict | None = None,
    ) -> dict:
        """Update an existing project YAML, commit, and push.

        - `users` replaces customer role bindings. Managed projects retain the
          provisioner `member` binding and assign customers `reader`; other
          projects use the self-service `member` shape.
        - `role_bindings` (kwarg) takes a fully-formed list and replaces the
          existing roleBindings verbatim — used by the cluster admin sync
          path to set `{role: reader, users: [...customer admins...]}` on
          managed projects.

        Returns the updated project dict.
        """
        with self._lock:
            self._pull()

            result = self._read_yaml(resource_name)
            if not result:
                raise ValueError(f"Project '{resource_name}' not found")

            file_path, doc = result
            spec = doc["spec"]
            changed = []

            if description is not None:
                spec["description"] = description
                changed.append("description")

            if quotas is not None:
                spec["quotas"] = quotas
                changed.append("quotas")

            if role_bindings is not None:
                spec["roleBindings"] = role_bindings
                changed.append("roleBindings")
            elif users is not None:
                if spec.get("managed", False):
                    spec["roleBindings"] = managed_role_bindings(self.settings, users)
                else:
                    spec["roleBindings"] = [
                        {
                            "role": "member",
                            "users": users,
                            "userDomain": self.settings.default_domain,
                        }
                    ]
                changed.append("users")

            self._write_yaml(file_path, doc)
            self._commit_and_push(
                f"Update project {spec.get('name', resource_name)} ({', '.join(changed)})"
            )

            return _parse_project(doc)

    def move_project(self, resource_name: str, new_contract_number: str) -> dict:
        """Reassign a project to a different contract, commit, and push.

        Managed projects are read-only and cannot be moved through this path.
        Only `spec.contractNumber` changes — the CR name (and thus the
        OpenStack project identity the operator keys on) is untouched, so the
        move is non-destructive. Returns the updated project dict.
        """
        with self._lock:
            self._pull()

            result = self._read_yaml(resource_name)
            if not result:
                raise ValueError(f"Project '{resource_name}' not found")

            file_path, doc = result
            spec = doc["spec"]
            require_project_mutable(_parse_project(doc))
            old_contract = spec.get("contractNumber", "")
            spec["contractNumber"] = new_contract_number

            self._write_yaml(file_path, doc)
            self._commit_and_push(
                f"Move project {spec.get('name', resource_name)} "
                f"from contract {old_contract} to {new_contract_number}"
            )

            return _parse_project(doc)

    def rename_contract(self, old_contract_number: str, new_contract_number: str) -> int:
        """Re-point every project of a contract to a new contract number.

        Rejects the entire operation before writing if any matching project is
        managed. Otherwise, rewrites `spec.contractNumber` from old to new
        across all matching project YAMLs in a single commit. CR names (the
        OpenStack project identity the operator keys on) are untouched, so
        this is non-destructive. Returns the number of projects re-pointed.
        """
        with self._lock:
            self._pull()

            matching_projects = []
            for path in self.projects_dir.glob("*.yaml"):
                with open(path) as f:
                    doc = yaml.safe_load(f)
                if not doc or doc.get("kind") != "OpenstackProject":
                    continue
                spec = doc.get("spec", {})
                if spec.get("contractNumber") != old_contract_number:
                    continue
                matching_projects.append((path, doc))

            require_contract_renamable([_parse_project(doc) for _, doc in matching_projects])

            for path, doc in matching_projects:
                spec = doc["spec"]
                spec["contractNumber"] = new_contract_number
                self._write_yaml(path, doc)

            if matching_projects:
                self._commit_and_push(
                    f"Rename contract {old_contract_number} to "
                    f"{new_contract_number} ({len(matching_projects)} project(s))"
                )

            return len(matching_projects)

    def delete_project(self, resource_name: str) -> None:
        """Delete a project YAML file, commit, and push."""
        with self._lock:
            self._pull()

            file_path = self.projects_dir / f"{resource_name}.yaml"
            if not file_path.exists():
                raise ValueError(f"Project '{resource_name}' not found")

            # Read name for commit message before deleting
            with open(file_path) as f:
                doc = yaml.safe_load(f)
            project_name = doc.get("spec", {}).get("name", resource_name) if doc else resource_name

            file_path.unlink()
            self._update_kustomization()
            self._commit_and_push(f"Delete project {project_name}")
