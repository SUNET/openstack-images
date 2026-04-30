"""Add tenant clusters, kubeconfig issuances, addons, requests, and billing seeds.

Revision ID: 008
Create Date: 2026-04-30
"""

import sqlalchemy as sa

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_cluster",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contract_id",
            sa.Integer(),
            sa.ForeignKey("contract.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("api_url", sa.String(512), nullable=False),
        sa.Column("ca_bundle", sa.Text(), nullable=False),
        sa.Column("openbao_mount", sa.String(255), nullable=False),
        sa.Column(
            "openbao_role",
            sa.String(255),
            nullable=False,
            server_default="argocd-rbac-manager",
        ),
        sa.Column(
            "argocd_role_name",
            sa.String(255),
            nullable=False,
            server_default="argocd-tenant",
        ),
        sa.Column(
            "argocd_namespace",
            sa.String(63),
            nullable=False,
            server_default="argocd",
        ),
        sa.Column("worker_groups", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "initial_worker_groups", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("provisioned_at", sa.DateTime(), nullable=True),
        sa.Column("management_project_resource_name", sa.String(253), nullable=True),
        sa.Column("backup_project_resource_name", sa.String(253), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("created_by_sub", sa.String(255), nullable=False),
    )

    op.create_table(
        "cluster_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cluster_id",
            sa.Integer(),
            sa.ForeignKey("tenant_cluster.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_sub", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("granted_by_sub", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("cluster_id", "user_sub", name="uq_cluster_user"),
    )

    op.create_table(
        "kubeconfig_issuance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cluster_id",
            sa.Integer(),
            sa.ForeignKey("tenant_cluster.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_sub", sa.String(255), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("cert_serial", sa.String(64), nullable=False),
        sa.Column("rolebinding_name", sa.String(253), nullable=False),
        sa.Column("cert_group", sa.String(253), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by_sub", sa.String(255), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_kubeconfig_issuance_cluster_user",
        "kubeconfig_issuance",
        ["cluster_id", "user_sub"],
    )

    op.create_table(
        "cluster_addon",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cluster_id",
            sa.Integer(),
            sa.ForeignKey("tenant_cluster.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("addon_type", sa.String(64), nullable=False),
        sa.Column("enabled_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("enabled_by_sub", sa.String(255), nullable=False),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_by_sub", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_cluster_addon_active_unique",
        "cluster_addon",
        ["cluster_id", "addon_type"],
        unique=True,
        postgresql_where=sa.text("disabled_at IS NULL"),
    )

    op.create_table(
        "cluster_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cluster_id",
            sa.Integer(),
            sa.ForeignKey("tenant_cluster.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_by_sub", sa.String(255), nullable=False),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("applied_by_sub", sa.String(255), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_cluster_request_cluster_status",
        "cluster_request",
        ["cluster_id", "status"],
    )

    # Seed default synthetic-resource prices.
    resource_price = sa.table(
        "resource_price",
        sa.column("resource_type", sa.String),
        sa.column("unit_price", sa.Numeric),
        sa.column("unit", sa.String),
        sa.column("metadata_field", sa.String),
        sa.column("metadata_value", sa.String),
    )
    op.bulk_insert(
        resource_price,
        [
            {
                "resource_type": "cluster_management_fee",
                "unit_price": 500,
                "unit": "vm-month",
                "metadata_field": None,
                "metadata_value": None,
            },
            {
                "resource_type": "cluster_setup_fee",
                "unit_price": 1000,
                "unit": "cluster",
                "metadata_field": "group_type",
                "metadata_value": "controllers",
            },
            {
                "resource_type": "cluster_setup_fee",
                "unit_price": 2000,
                "unit": "worker-group",
                "metadata_field": "group_type",
                "metadata_value": "workers",
            },
            {
                "resource_type": "cluster_addon_fee",
                "unit_price": 3450,
                "unit": "month",
                "metadata_field": "addon",
                "metadata_value": "jupyterhub",
            },
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM resource_price WHERE resource_type IN ("
        "'cluster_management_fee', 'cluster_setup_fee', 'cluster_addon_fee')"
    )
    op.drop_index("ix_cluster_request_cluster_status", table_name="cluster_request")
    op.drop_table("cluster_request")
    op.drop_index("ix_cluster_addon_active_unique", table_name="cluster_addon")
    op.drop_table("cluster_addon")
    op.drop_index("ix_kubeconfig_issuance_cluster_user", table_name="kubeconfig_issuance")
    op.drop_table("kubeconfig_issuance")
    op.drop_table("cluster_access")
    op.drop_table("tenant_cluster")
