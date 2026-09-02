"""Allow tenant clusters to be planned before Kubernetes exists.

Revision ID: 011
Revises: 010
Create Date: 2026-09-02
"""

import sqlalchemy as sa

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tenant_cluster",
        "api_url",
        existing_type=sa.String(length=512),
        nullable=True,
    )
    op.alter_column(
        "tenant_cluster",
        "ca_bundle",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    # Old application versions require values even for plans. Preserve rows
    # during rollback with unmistakable non-functional sentinels that can be
    # replaced through the existing cluster update endpoint.
    op.execute(
        "UPDATE tenant_cluster "
        "SET api_url = 'https://unconfigured.invalid' "
        "WHERE api_url IS NULL"
    )
    op.execute(
        "UPDATE tenant_cluster "
        "SET ca_bundle = 'UNCONFIGURED' "
        "WHERE ca_bundle IS NULL"
    )
    op.alter_column(
        "tenant_cluster",
        "ca_bundle",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "tenant_cluster",
        "api_url",
        existing_type=sa.String(length=512),
        nullable=False,
    )
