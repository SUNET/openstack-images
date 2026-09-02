"""Application configuration from environment variables."""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _required_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return val


def _validated_base_url() -> str:
    """BASE_URL is required and must be an https origin (http://localhost ok in dev)."""
    val = _required_env("BASE_URL").rstrip("/")
    parsed = urlparse(val)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"BASE_URL must be a full URL (got {val!r})")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        if os.environ.get("PORTAL_ALLOW_INSECURE_BASE_URL", "") != "1":
            raise RuntimeError(
                "BASE_URL must use https; set PORTAL_ALLOW_INSECURE_BASE_URL=1 "
                "to override (dev only)"
            )
    return val


def _validated_openbao_addr() -> str:
    val = os.environ.get("OPENBAO_ADDR", "https://openbao.openbao.svc.cluster.local:8200")
    parsed = urlparse(val)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"OPENBAO_ADDR must be a full URL (got {val!r})")
    if parsed.scheme == "http" and os.environ.get("OPENBAO_ALLOW_INSECURE", "") != "1":
        raise RuntimeError(
            "OPENBAO_ADDR uses http; set OPENBAO_ALLOW_INSECURE=1 to override (dev only)"
        )
    return val


def _split_csv(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


def _cluster_dns_zone() -> str:
    default = (
        "k8s-test.sunetvdc.se"
        if os.environ.get("PORTAL_IS_IN_TEST", "").strip().lower() in ("1", "true", "yes", "on")
        else "k8s-prod.sunetvdc.se"
    )
    value = os.environ.get("CLUSTER_DNS_ZONE", default).strip().rstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise RuntimeError(f"CLUSTER_DNS_ZONE is not a valid DNS name (got {value!r})")
    return value


@dataclass(frozen=True)
class Settings:
    # OIDC
    oidc_issuer: str = field(default_factory=lambda: _required_env("OIDC_ISSUER"))
    oidc_client_id: str = field(default_factory=lambda: _required_env("OIDC_CLIENT_ID"))
    oidc_client_secret: str = field(default_factory=lambda: _required_env("OIDC_CLIENT_SECRET"))
    oidc_redirect_uri: str = field(default_factory=lambda: _required_env("OIDC_REDIRECT_URI"))

    # Session
    secret_key: str = field(default_factory=lambda: _required_env("SECRET_KEY"))
    # SameSite=lax works with the OIDC redirect callback; strict would drop
    # the session on the cross-site auth callback bounce.
    session_cookie_name: str = field(
        default_factory=lambda: os.environ.get("SESSION_COOKIE_NAME", "portal_session")
    )
    session_max_age_seconds: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(8 * 3600)))
    )
    session_https_only: bool = field(
        default_factory=lambda: os.environ.get("SESSION_HTTPS_ONLY", "1") == "1"
    )

    # Database
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://portal:portal@localhost:5432/portal"
        )
    )

    # OpenStack project desired-state Git backend
    project_git_repo_url: str = field(
        default_factory=lambda: _required_env("PROJECT_GIT_REPO_URL")
    )
    project_git_username: str = field(
        default_factory=lambda: _required_env("PROJECT_GIT_USERNAME")
    )
    project_git_token: str = field(
        default_factory=lambda: _required_env("PROJECT_GIT_TOKEN")
    )
    project_git_branch: str = field(
        default_factory=lambda: os.environ.get("PROJECT_GIT_BRANCH", "main")
    )
    project_git_work_dir: str = field(
        default_factory=lambda: os.environ.get(
            "PROJECT_GIT_WORK_DIR", "/tmp/customer-projects"
        )
    )
    git_author_name: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_NAME", "Customer Portal")
    )
    git_author_email: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_EMAIL", "portal@sunet.se")
    )

    # Desired-state repository for managed customer Kubernetes clusters.
    # Empty keeps existing deployments bootable, but planned cluster creation
    # returns 503 until the repository is configured.
    cluster_git_repo_url: str = field(
        default_factory=lambda: os.environ.get("CLUSTER_GIT_REPO_URL", "").strip()
    )
    cluster_git_username: str = field(
        default_factory=lambda: os.environ.get("CLUSTER_GIT_USERNAME", "").strip()
    )
    cluster_git_token: str = field(
        default_factory=lambda: os.environ.get("CLUSTER_GIT_TOKEN", "").strip()
    )
    cluster_git_branch: str = field(
        default_factory=lambda: os.environ.get("CLUSTER_GIT_BRANCH", "main")
    )
    cluster_git_work_dir: str = field(
        default_factory=lambda: os.environ.get("CLUSTER_GIT_WORK_DIR", "/tmp/customer-clusters")
    )
    cluster_dns_zone: str = field(default_factory=_cluster_dns_zone)

    # Portal
    admin_users: list[str] = field(default_factory=lambda: _split_csv("PORTAL_ADMIN_USERS"))
    base_url: str = field(default_factory=_validated_base_url)
    is_test: bool = field(
        default_factory=lambda: (
            os.environ.get("PORTAL_IS_IN_TEST", "").strip().lower() in ("1", "true", "yes", "on")
        )
    )

    # OpenStack project defaults
    default_domain: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_DOMAIN", "sso-users")
    )
    federation_config_map: str = field(
        default_factory=lambda: os.environ.get("FEDERATION_CONFIGMAP", "federation-config")
    )
    federation_config_namespace: str = field(
        default_factory=lambda: os.environ.get(
            "FEDERATION_CONFIGMAP_NAMESPACE", "openstack-operator"
        )
    )

    # SMTP (for billing email delivery)
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: os.environ.get("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM", "portal@sunet.se"))

    # Billing
    billing_trigger_token: str = field(
        default_factory=lambda: os.environ.get("BILLING_TRIGGER_TOKEN", "")
    )
    openstack_cloud: str = field(
        default_factory=lambda: os.environ.get("OPENSTACK_CLOUD", "openstack")
    )
    # WebDAV delivery: explicit allowlist of hostnames the portal may PUT to.
    # Empty list disables WebDAV delivery.
    webdav_allowed_hosts: list[str] = field(
        default_factory=lambda: _split_csv("WEBDAV_ALLOWED_HOSTS")
    )

    # OpenBao (for tenant cluster ephemeral RBAC creds)
    openbao_addr: str = field(default_factory=_validated_openbao_addr)
    openbao_k8s_auth_role: str = field(
        default_factory=lambda: os.environ.get("OPENBAO_K8S_AUTH_ROLE", "customer-portal")
    )
    openbao_sa_token_path: str = field(
        default_factory=lambda: os.environ.get(
            "OPENBAO_SA_TOKEN_PATH",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        )
    )
    openbao_ca_path: str = field(default_factory=lambda: os.environ.get("OPENBAO_CA_PATH", ""))

    # Tenant kubeconfig
    default_kubeconfig_ttl_days: int = field(
        default_factory=lambda: int(os.environ.get("DEFAULT_KUBECONFIG_TTL_DAYS", "90"))
    )

    # Cluster-request notifications
    sunet_ops_email: str = field(
        default_factory=lambda: os.environ.get("SUNET_OPS_EMAIL", "drift@sunet.se")
    )


def get_settings() -> Settings:
    return Settings()
