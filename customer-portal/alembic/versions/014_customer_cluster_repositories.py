"""Add per-customer environment GitOps repository configuration.

Revision ID: 014
Revises: 013
Create Date: 2026-09-15
"""

import sqlalchemy as sa

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_cluster_repository",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customer.id"), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("repo_url", sa.String(length=2048), nullable=False),
        sa.Column("writer_username", sa.String(length=255), nullable=False),
        sa.Column("reader_username", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("customer_id", "environment", name="uq_customer_cluster_repository"),
    )


def downgrade() -> None:
    op.drop_table("customer_cluster_repository")
