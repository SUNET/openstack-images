"""Add preserved managed-cluster decommissioning state.

Revision ID: 018
Revises: 017
"""

import sqlalchemy as sa

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_cluster", sa.Column("decommissioned_at", sa.DateTime()))
    op.add_column(
        "tenant_cluster",
        sa.Column("decommissioned_by_sub", sa.String(length=255)),
    )
    op.add_column("tenant_cluster", sa.Column("decommission_note", sa.Text()))


def downgrade() -> None:
    op.drop_column("tenant_cluster", "decommission_note")
    op.drop_column("tenant_cluster", "decommissioned_by_sub")
    op.drop_column("tenant_cluster", "decommissioned_at")
