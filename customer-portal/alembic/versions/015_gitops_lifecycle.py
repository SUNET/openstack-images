"""Add repository credential versions and durable cluster GitOps operations.

Revision ID: 015
Revises: 014
"""

import sqlalchemy as sa

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_cluster", sa.Column(
        "config_version", sa.Integer(), nullable=False, server_default="1"
    ))
    for column in (
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("writer_secret_version", sa.Integer()),
        sa.Column("reader_secret_version", sa.Integer()),
        sa.Column("writer_updated_at", sa.DateTime()),
        sa.Column("reader_updated_at", sa.DateTime()),
        sa.Column("validated_at", sa.DateTime()),
        sa.Column(
            "validation_status", sa.String(32), nullable=False, server_default="unvalidated"
        ),
        sa.Column("validation_message", sa.String(512)),
    ):
        op.add_column("customer_cluster_repository", column)
    op.create_table(
        "cluster_gitops",
        sa.Column(
            "cluster_id", sa.Integer(), sa.ForeignKey("tenant_cluster.id"), primary_key=True
        ),
        sa.Column("repository_id", sa.Integer(),
                  sa.ForeignKey("customer_cluster_repository.id"), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("acme_contact", sa.String(254)),
        sa.Column("baseline", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("last_commit", sa.String(64)),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("reader_installed_version", sa.Integer()),
    )
    op.create_table(
        "gitops_operation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("tenant_cluster.id"), nullable=False),
        sa.Column("repository_id", sa.Integer(),
                  sa.ForeignKey("customer_cluster_repository.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requested_by_sub", sa.String(255), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_commit", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(512)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
    )
    op.create_index("ix_gitops_operation_status", "gitops_operation", ["status"])


def downgrade() -> None:
    op.drop_table("gitops_operation")
    op.drop_table("cluster_gitops")
    op.drop_column("tenant_cluster", "config_version")
    for name in (
        "validation_message", "validation_status", "validated_at", "reader_updated_at",
        "writer_updated_at", "reader_secret_version", "writer_secret_version", "version",
    ):
        op.drop_column("customer_cluster_repository", name)
