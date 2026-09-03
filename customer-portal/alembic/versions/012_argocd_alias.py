"""Add optional Argo CD DNS alias metadata.

Revision ID: 012
Revises: 011
Create Date: 2026-09-03
"""

import sqlalchemy as sa

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_cluster",
        sa.Column("argocd_alias", sa.String(length=253), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_cluster", "argocd_alias")
