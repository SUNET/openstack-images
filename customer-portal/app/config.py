"""Application configuration from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # OIDC
    oidc_issuer: str = field(default_factory=lambda: os.environ["OIDC_ISSUER"])
    oidc_client_id: str = field(default_factory=lambda: os.environ["OIDC_CLIENT_ID"])
    oidc_client_secret: str = field(default_factory=lambda: os.environ["OIDC_CLIENT_SECRET"])
    oidc_redirect_uri: str = field(default_factory=lambda: os.environ["OIDC_REDIRECT_URI"])

    # Session
    secret_key: str = field(default_factory=lambda: os.environ["SECRET_KEY"])

    # Database
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://portal:portal@localhost:5432/portal"
        )
    )

    # Git backend
    git_repo_url: str = field(default_factory=lambda: os.environ["GIT_REPO_URL"])
    git_branch: str = field(default_factory=lambda: os.environ.get("GIT_BRANCH", "main"))
    git_work_dir: str = field(
        default_factory=lambda: os.environ.get("GIT_WORK_DIR", "/tmp/customer-projects")
    )
    git_author_name: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_NAME", "Customer Portal")
    )
    git_author_email: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_EMAIL", "portal@sunet.se")
    )

    # Portal
    admin_users: list[str] = field(
        default_factory=lambda: [
            u.strip()
            for u in os.environ.get("PORTAL_ADMIN_USERS", "").split(",")
            if u.strip()
        ]
    )
    base_url: str = field(default_factory=lambda: os.environ.get("BASE_URL", ""))
    is_test: bool = field(
        default_factory=lambda: os.environ.get("PORTAL_IS_IN_TEST", "").strip().lower()
        in ("1", "true", "yes", "on")
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
    smtp_from: str = field(
        default_factory=lambda: os.environ.get("SMTP_FROM", "portal@sunet.se")
    )

    # Billing
    billing_trigger_token: str = field(
        default_factory=lambda: os.environ.get("BILLING_TRIGGER_TOKEN", "")
    )
    openstack_cloud: str = field(
        default_factory=lambda: os.environ.get("OPENSTACK_CLOUD", "openstack")
    )

    # OpenBao (for tenant cluster ephemeral RBAC creds)
    openbao_addr: str = field(
        default_factory=lambda: os.environ.get(
            "OPENBAO_ADDR", "http://openbao.openbao.svc.cluster.local:8200"
        )
    )
    openbao_k8s_auth_role: str = field(
        default_factory=lambda: os.environ.get("OPENBAO_K8S_AUTH_ROLE", "customer-portal")
    )
    openbao_sa_token_path: str = field(
        default_factory=lambda: os.environ.get(
            "OPENBAO_SA_TOKEN_PATH",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        )
    )

    # Tenant kubeconfig
    default_kubeconfig_ttl_days: int = field(
        default_factory=lambda: int(os.environ.get("DEFAULT_KUBECONFIG_TTL_DAYS", "365"))
    )

    # Cluster-request notifications
    sunet_ops_email: str = field(
        default_factory=lambda: os.environ.get("SUNET_OPS_EMAIL", "drift@sunet.se")
    )


def get_settings() -> Settings:
    return Settings()
