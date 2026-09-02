"""Git backend for managed customer cluster desired-state manifests."""

import logging
import threading
from pathlib import Path

import git
import yaml

from app.config import Settings
from app.git_url import git_auth_environment

logger = logging.getLogger(__name__)


class ClusterGitBackend:
    """Manage one declarative manifest per planned customer cluster."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_dir = Path(settings.cluster_git_work_dir)
        self.clusters_dir = self.work_dir / "clusters"
        self.repo: git.Repo | None = None
        self._lock = threading.Lock()
        self._auth_env = git_auth_environment(
            settings.cluster_git_repo_url,
            settings.cluster_git_username,
            settings.cluster_git_token,
        )

    def init(self) -> None:
        """Clone or update the configured cluster repository."""
        if not self.settings.cluster_git_repo_url:
            raise RuntimeError("CLUSTER_GIT_REPO_URL is not configured")
        if (self.work_dir / ".git").exists():
            self.repo = git.Repo(self.work_dir)
            self.repo.remotes.origin.set_url(self.settings.cluster_git_repo_url)
            with self.repo.git.custom_environment(**self._auth_env):
                self.repo.remotes.origin.pull(self.settings.cluster_git_branch)
        else:
            self.repo = git.Repo.clone_from(
                self.settings.cluster_git_repo_url,
                self.work_dir,
                branch=self.settings.cluster_git_branch,
                env=self._auth_env,
            )
        self.clusters_dir.mkdir(exist_ok=True)

    def _pull(self) -> None:
        if self.repo is None:
            raise RuntimeError("Cluster git repository is not initialized")
        with self.repo.git.custom_environment(**self._auth_env):
            self.repo.remotes.origin.pull(self.settings.cluster_git_branch)

    def _commit_and_push(self, message: str, max_retries: int = 3) -> None:
        if self.repo is None:
            raise RuntimeError("Cluster git repository is not initialized")
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
                    results = self.repo.remotes.origin.push(
                        self.settings.cluster_git_branch
                    )
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
                return
            except git.GitCommandError:
                if attempt == max_retries - 1:
                    raise
                logger.warning(
                    "Cluster manifest push failed (attempt %d); rebasing",
                    attempt + 1,
                )
                with self.repo.git.custom_environment(**self._auth_env):
                    self.repo.git.pull(
                        "--rebase",
                        "origin",
                        self.settings.cluster_git_branch,
                    )

    def _restore_origin(self) -> None:
        """Discard failed local publication state in the disposable clone."""
        if self.repo is None:
            return
        try:
            self.repo.git.rebase("--abort")
        except git.GitCommandError:
            pass
        self.repo.git.reset(
            "--hard",
            f"origin/{self.settings.cluster_git_branch}",
        )
        self.repo.git.clean("-fd")

    def _manifest_path(self, slug: str) -> Path:
        return self.clusters_dir / slug / "cluster.yaml"

    def exists(self, slug: str) -> bool:
        """Return whether the cluster already has a manifest."""
        with self._lock:
            self._pull()
            return self._manifest_path(slug).exists()

    def write_cluster(
        self,
        *,
        slug: str,
        display_name: str,
        contract_number: str,
        customer_domain: str,
        worker_groups: int,
        project_name: str,
        project_resource_name: str,
    ) -> str:
        """Write and push the desired-state manifest, returning its path."""
        with self._lock:
            self._pull()
            path = self._manifest_path(slug)
            relative_path = path.relative_to(self.work_dir).as_posix()
            document = {
                "apiVersion": "customer-clusters.sunet.se/v1alpha1",
                "kind": "ManagedCluster",
                "metadata": {"name": slug},
                "spec": {
                    "displayName": display_name,
                    "contractNumber": contract_number,
                    "customerDomain": customer_domain,
                    "workerGroups": worker_groups,
                    "openstack": {
                        "projectName": project_name,
                        "projectResourceName": project_resource_name,
                    },
                    "dns": {
                        "zone": self.settings.cluster_dns_zone,
                        "apiHostname": (
                            f"api.{slug}.{self.settings.cluster_dns_zone}"
                        ),
                        "argocdHostname": (
                            f"argocd.{slug}.{self.settings.cluster_dns_zone}"
                        ),
                    },
                    "openbao": {
                        "mount": f"kubernetes/{slug}",
                        "secretRoot": f"kv/customer-clusters/{slug}",
                    },
                },
            }
            if path.exists():
                existing = yaml.safe_load(path.read_text())
                if existing == document:
                    return relative_path
                raise ValueError(f"Cluster manifest '{slug}' already exists")

            try:
                path.parent.mkdir(parents=True)
                path.write_text(
                    "---\n"
                    + yaml.dump(
                        document,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                )
                self._commit_and_push(f"Add planned customer cluster {slug}")
            except Exception:
                try:
                    self._restore_origin()
                except Exception:
                    logger.exception("Failed to restore cluster repository clone")
                raise
            return relative_path

    def delete_cluster(self, slug: str) -> None:
        """Delete and push one cluster manifest."""
        with self._lock:
            self._pull()
            path = self._manifest_path(slug)
            if not path.exists():
                raise ValueError(f"Cluster manifest '{slug}' not found")
            try:
                path.unlink()
                path.parent.rmdir()
                self._commit_and_push(f"Remove customer cluster {slug}")
            except Exception:
                try:
                    self._restore_origin()
                except Exception:
                    logger.exception("Failed to restore cluster repository clone")
                raise
