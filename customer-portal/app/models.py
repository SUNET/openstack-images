"""SQLAlchemy ORM models for the customer portal."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    contracts: Mapped[list["Contract"]] = relationship(back_populates="customer")


class Contract(Base):
    __tablename__ = "contract"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), nullable=False)
    contract_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    customer: Mapped["Customer"] = relationship(back_populates="contracts")
    access_grants: Mapped[list["ContractAccess"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )
    price_overrides: Mapped[list["ContractPriceOverride"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )
    rebate: Mapped["ContractRebate | None"] = relationship(
        back_populates="contract", uselist=False, cascade="all, delete-orphan"
    )


class ContractAccess(Base):
    __tablename__ = "contract_access"
    __table_args__ = (
        UniqueConstraint("contract_id", "user_sub", name="uq_contract_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), nullable=False)
    user_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    contract: Mapped["Contract"] = relationship(back_populates="access_grants")


class ResourcePrice(Base):
    """Global default price per resource type, optionally scoped to a metadata value."""

    __tablename__ = "resource_price"
    __table_args__ = (
        UniqueConstraint(
            "resource_type", "metadata_field", "metadata_value",
            name="uq_resource_price_type_meta",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    metadata_field: Mapped[str | None] = mapped_column(String(100))
    metadata_value: Mapped[str | None] = mapped_column(String(255))


class ContractPriceOverride(Base):
    """Per-contract price override for a resource type."""

    __tablename__ = "contract_price_override"
    __table_args__ = (
        UniqueConstraint("contract_id", "resource_type", name="uq_contract_resource_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    contract: Mapped["Contract"] = relationship(back_populates="price_overrides")


class ContractRebate(Base):
    """Per-contract rebate percentage."""

    __tablename__ = "contract_rebate"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contract.id"), unique=True, nullable=False
    )
    rebate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    contract: Mapped["Contract"] = relationship(back_populates="rebate")


class BillingJob(Base):
    """Configured billing export job."""

    __tablename__ = "billing_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    all_contracts: Mapped[bool] = mapped_column(default=False)
    schedule: Mapped[str] = mapped_column(String(100), nullable=False)
    delivery_method: Mapped[str] = mapped_column(String(50), nullable=False)
    delivery_config: Mapped[str] = mapped_column(Text, nullable=False)
    filename_template: Mapped[str] = mapped_column(
        String(255), default="billing-{year}-{month}.csv"
    )
    per_contract: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    selected_contracts: Mapped[list["BillingJobContract"]] = relationship(
        back_populates="billing_job", cascade="all, delete-orphan"
    )
    runs: Mapped[list["BillingJobRun"]] = relationship(
        back_populates="billing_job", cascade="all, delete-orphan"
    )


class BillingJobContract(Base):
    """Junction table for billing job contract selection."""

    __tablename__ = "billing_job_contract"
    __table_args__ = (
        UniqueConstraint("billing_job_id", "contract_id", name="uq_billing_job_contract"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_job_id: Mapped[int] = mapped_column(
        ForeignKey("billing_job.id", ondelete="CASCADE"), nullable=False
    )
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), nullable=False
    )

    billing_job: Mapped["BillingJob"] = relationship(back_populates="selected_contracts")
    contract: Mapped["Contract"] = relationship()


class BillingJobRun(Base):
    """Execution history for billing jobs."""

    __tablename__ = "billing_job_run"
    __table_args__ = (
        Index(
            "uq_billing_job_run_active_period",
            "billing_job_id",
            "billing_period_start",
            "billing_period_end",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_job_id: Mapped[int] = mapped_column(
        ForeignKey("billing_job.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    billing_period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    billing_period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error_message: Mapped[str | None] = mapped_column(Text)
    files_delivered: Mapped[int] = mapped_column(default=0)

    billing_job: Mapped["BillingJob"] = relationship(back_populates="runs")
    report: Mapped["BillingReport | None"] = relationship(
        back_populates="billing_job_run", uselist=False, passive_deletes=True
    )


class BillingReport(Base):
    """Durable asynchronous billing report and downloadable artifact."""

    __tablename__ = "billing_report"
    __table_args__ = (
        Index("ix_billing_report_queue", "status", "created_at"),
        Index("ix_billing_report_owner", "requested_by_sub", "created_at"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'expired')",
            name="ck_billing_report_status",
        ),
        CheckConstraint(
            "(delivery_method IS NULL AND delivery_config IS NULL) OR "
            "(delivery_method IN ('webdav', 'email') AND "
            "(delivery_config IS NOT NULL OR status IN ('succeeded', 'expired')))",
            name="ck_billing_report_delivery",
        ),
        UniqueConstraint("billing_job_run_id", name="uq_billing_report_job_run"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    billing_job_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("billing_job_run.id", ondelete="CASCADE")
    )
    requested_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    billing_period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    billing_period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    contract_numbers_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot_json: Mapped[str | None] = mapped_column(Text)
    filename_template: Mapped[str] = mapped_column(String(255), nullable=False)
    per_contract: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivery_method: Mapped[str | None] = mapped_column(String(50))
    delivery_config: Mapped[str | None] = mapped_column(Text)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_filename: Mapped[str | None] = mapped_column(String(255))
    result_media_type: Mapped[str | None] = mapped_column(String(128))
    result_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    result_size: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    billing_job_run: Mapped["BillingJobRun | None"] = relationship(
        back_populates="report"
    )
    shards: Mapped[list["BillingReportShard"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )
    outputs: Mapped[list["BillingReportOutput"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class BillingReportShard(Base):
    """Checkpoint for one bounded product/project/time-window query."""

    __tablename__ = "billing_report_shard"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "metric",
            "project_id",
            "window_start",
            "window_end",
            name="uq_billing_report_shard_window",
        ),
        Index("ix_billing_report_shard_pending", "report_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("billing_report.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    usage_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(String(512))

    report: Mapped["BillingReport"] = relationship(back_populates="shards")


class BillingReportOutput(Base):
    """Immutable report file with a durable external-delivery checkpoint."""

    __tablename__ = "billing_report_output"
    __table_args__ = (
        UniqueConstraint(
            "report_id", "filename", name="uq_billing_report_output_filename"
        ),
        Index("ix_billing_report_output_pending", "report_id", "status", "id"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'sent')",
            name="ck_billing_report_output_status",
        ),
        CheckConstraint("size >= 0", name="ck_billing_report_output_size"),
        CheckConstraint(
            "(status = 'sent' AND delivered_at IS NOT NULL) OR "
            "(status IN ('pending', 'ready') AND delivered_at IS NULL)",
            name="ck_billing_report_output_delivery",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("billing_report.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(String(512))

    report: Mapped["BillingReport"] = relationship(back_populates="outputs")


class TenantCluster(Base):
    """A managed Kubernetes cluster owned by SUNET, allocated to a contract."""

    __tablename__ = "tenant_cluster"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    api_url: Mapped[str | None] = mapped_column(String(512))
    ca_bundle: Mapped[str | None] = mapped_column(Text)
    openbao_mount: Mapped[str] = mapped_column(String(255), nullable=False)
    openbao_role: Mapped[str] = mapped_column(
        String(255), nullable=False, default="argocd-rbac-manager"
    )
    argocd_role_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="argocd-tenant"
    )
    argocd_namespace: Mapped[str] = mapped_column(
        String(63), nullable=False, default="argocd"
    )
    argocd_alias: Mapped[str | None] = mapped_column(String(253))
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    worker_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    initial_worker_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime)

    management_project_resource_name: Mapped[str | None] = mapped_column(String(253))
    backup_project_resource_name: Mapped[str | None] = mapped_column(String(253))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)

    contract: Mapped["Contract"] = relationship()
    access_grants: Mapped[list["ClusterAccess"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )
    issuances: Mapped[list["KubeconfigIssuance"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )
    addons: Mapped[list["ClusterAddon"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )
    requests: Mapped[list["ClusterRequest"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )


class CustomerClusterRepository(Base):
    """One private cluster GitOps repository per customer and environment.

    Authentication tokens deliberately live only in OpenBao.  This table keeps
    the non-secret repository address and bot identity needed to use them.
    """

    __tablename__ = "customer_cluster_repository"
    __table_args__ = (
        UniqueConstraint("customer_id", "environment", name="uq_customer_cluster_repository"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    repo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    writer_username: Mapped[str] = mapped_column(String(255), nullable=False)
    reader_username: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    writer_secret_version: Mapped[int | None] = mapped_column(Integer)
    reader_secret_version: Mapped[int | None] = mapped_column(Integer)
    writer_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    reader_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime)
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unvalidated", server_default="unvalidated"
    )
    validation_message: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    customer: Mapped["Customer"] = relationship()


class ClusterGitOps(Base):
    """Stable repository association, pending settings and last published baseline."""

    __tablename__ = "cluster_gitops"

    cluster_id: Mapped[int] = mapped_column(ForeignKey("tenant_cluster.id"), primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("customer_cluster_repository.id"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    acme_contact: Mapped[str | None] = mapped_column(String(254))
    baseline: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    last_commit: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    reader_installed_version: Mapped[int | None] = mapped_column(Integer)


class GitOpsOperation(Base):
    """Durable, non-secret preview/publication intent consumed under database locks."""

    __tablename__ = "gitops_operation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("tenant_cluster.id"), nullable=False)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("customer_cluster_repository.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    requested_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    result_commit: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ClusterAccess(Base):
    """User → cluster grant. role: 'customer_admin' or 'user'."""

    __tablename__ = "cluster_access"
    __table_args__ = (
        UniqueConstraint("cluster_id", "user_sub", name="uq_cluster_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_cluster.id", ondelete="CASCADE"), nullable=False
    )
    user_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    cluster: Mapped["TenantCluster"] = relationship(back_populates="access_grants")


class KubeconfigIssuance(Base):
    """One issued kubeconfig (one cert + one RoleBinding) on a cluster for a user."""

    __tablename__ = "kubeconfig_issuance"
    __table_args__ = (
        Index("ix_kubeconfig_issuance_cluster_user", "cluster_id", "user_sub"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_cluster.id", ondelete="CASCADE"), nullable=False
    )
    user_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    cert_serial: Mapped[str] = mapped_column(String(64), nullable=False)
    rolebinding_name: Mapped[str] = mapped_column(String(253), nullable=False)
    cert_group: Mapped[str] = mapped_column(String(253), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_by_sub: Mapped[str | None] = mapped_column(String(255))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    cluster: Mapped["TenantCluster"] = relationship(back_populates="issuances")


class ClusterAddon(Base):
    """Active addon installation on a cluster (one row per enable cycle).

    `disabled_at IS NULL` ⇔ currently active. The partial unique index in the
    alembic migration ensures only one active row per (cluster, addon_type).
    """

    __tablename__ = "cluster_addon"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_cluster.id", ondelete="CASCADE"), nullable=False
    )
    addon_type: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    enabled_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_by_sub: Mapped[str | None] = mapped_column(String(255))

    cluster: Mapped["TenantCluster"] = relationship(back_populates="addons")


class ClusterRequest(Base):
    """Customer-admin-initiated change request, applied/denied by a SUNET admin.

    Doubles as the audit log: applied resize requests are read by the billing
    engine to emit per-period worker-group setup fees.
    """

    __tablename__ = "cluster_request"
    __table_args__ = (
        Index("ix_cluster_request_cluster_status", "cluster_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_cluster.id", ondelete="CASCADE"), nullable=False
    )
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_by_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    applied_by_sub: Mapped[str | None] = mapped_column(String(255))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)

    cluster: Mapped["TenantCluster"] = relationship(back_populates="requests")
