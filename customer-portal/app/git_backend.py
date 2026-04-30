"""Git backend for managing OpenstackProject YAML files."""

import logging
import re
import threading
from pathlib import Path

import git
import yaml

from app.config import Settings

logger = logging.getLogger(__name__)

# Default quotas for self-service projects
DEFAULT_QUOTAS = {
    "compute": {"instances": 10, "cores": 20, "ramMB": 40960},
    "storage": {"volumes": 10, "volumesGB": 500, "snapshots": 10},
    "network": {"securityGroups": 10, "securityGroupRules": 100},
}


def _sanitize_name(name: str) -> str:
    """Sanitize a project name for use as a K8s resource name and filename."""
    sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized[:63]


def _parse_project(doc: dict) -> dict:
    """Extract project info from a parsed YAML document."""
    spec = doc.get("spec", {})
    resource_name = doc.get("metadata", {}).get("name", "")
    role_bindings = spec.get("roleBindings", [])
    users = []
    for rb in role_bindings:
        users.extend(rb.get("users", []))
    return {
        "resource_name": resource_name,
        "name": spec.get("name", ""),
        "description": spec.get("description", ""),
        "contract_number": spec.get("contractNumber", ""),
        "users": users,
        "managed": bool(spec.get("managed", False)),
    }


class GitBackend:
    """Manages OpenstackProject YAML files in a git repository."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_dir = Path(settings.git_work_dir)
        self.projects_dir = self.work_dir / "projects"
        self.repo: git.Repo | None = None
        self._lock = threading.Lock()

    def init(self) -> None:
        """Clone or pull the repository."""
        if (self.work_dir / ".git").exists():
            self.repo = git.Repo(self.work_dir)
            self.repo.remotes.origin.pull(self.settings.git_branch)
            logger.info("Pulled latest changes from %s", self.settings.git_repo_url)
        else:
            self.repo = git.Repo.clone_from(
                self.settings.git_repo_url,
                self.work_dir,
                branch=self.settings.git_branch,
            )
            logger.info("Cloned %s to %s", self.settings.git_repo_url, self.work_dir)

        self.projects_dir.mkdir(exist_ok=True)

    def _pull(self) -> None:
        """Pull latest changes before writing."""
        if self.repo:
            self.repo.remotes.origin.pull(self.settings.git_branch)

    def _commit_and_push(self, message: str, max_retries: int = 3) -> None:
        """Stage all changes, commit, and push with retry on conflict."""
        if self.repo is None:
            raise RuntimeError("Git repo not initialized")

        self.repo.git.add("-A")
        self.repo.index.commit(
            message,
            author=git.Actor(self.settings.git_author_name, self.settings.git_author_email),
            committer=git.Actor(self.settings.git_author_name, self.settings.git_author_email),
        )

        for attempt in range(max_retries):
            try:
                self.repo.remotes.origin.push(self.settings.git_branch)
                logger.info("Pushed: %s", message)
                return
            except git.GitCommandError:
                if attempt < max_retries - 1:
                    logger.warning("Push failed (attempt %d), rebasing and retrying", attempt + 1)
                    self.repo.git.pull("--rebase", "origin", self.settings.git_branch)
                else:
                    raise

    def _update_kustomization(self) -> None:
        """Update kustomization.yaml to list all project files."""
        kustomization_path = self.work_dir / "kustomization.yaml"
        project_files = sorted(
            f"projects/{p.name}" for p in self.projects_dir.glob("*.yaml")
        )

        kustomization = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": project_files,
        }

        with open(kustomization_path, "w") as f:
            yaml.dump(kustomization, f, default_flow_style=False, allow_unicode=True)

    def _render_project_cr(
        self,
        contract_number: str,
        project_name: str,
        description: str,
        users: list[str],
        *,
        managed: bool = False,
    ) -> dict:
        """Render an OpenstackProject CR as a dict."""
        resource_name = _sanitize_name(project_name)

        spec: dict = {
            "name": project_name,
            "domain": self.settings.default_domain,
            "description": description,
            "enabled": True,
            "contractNumber": contract_number,
            "quotas": DEFAULT_QUOTAS,
            "roleBindings": [
                {
                    "role": "member",
                    "users": users,
                    "userDomain": self.settings.default_domain,
                }
            ],
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
            yaml.dump(doc, f, default_flow_style=False, allow_unicode=True)

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
    ) -> str:
        """Create an OpenstackProject YAML file, commit, and push.

        Returns the sanitized resource name. When `managed=True`, the CR is
        marked SUNET-managed: the operator assigns customer-domain users the
        Keystone `reader` role rather than `member`, and portal-side mutation
        endpoints reject non-admin write attempts.
        """
        with self._lock:
            self._pull()

            resource_name = _sanitize_name(project_name)
            file_path = self.projects_dir / f"{resource_name}.yaml"

            if file_path.exists():
                raise ValueError(f"Project '{resource_name}' already exists")

            cr = self._render_project_cr(
                contract_number, project_name, description, users, managed=managed
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
    ) -> dict:
        """Update an existing project YAML, commit, and push.

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

            if users is not None:
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
