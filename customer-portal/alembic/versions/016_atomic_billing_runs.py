"""Prevent concurrent billing runs for the same job and period.

Revision ID: 016
Revises: 015
"""

import sqlalchemy as sa

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_billing_job_run_active_period"


def upgrade() -> None:
    """Close duplicate active runs before enforcing single ownership."""
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY billing_job_id,
                                        billing_period_start,
                                        billing_period_end
                           ORDER BY id DESC
                       ) AS position
                FROM billing_job_run
                WHERE status = 'running'
            )
            UPDATE billing_job_run AS duplicate
            SET status = 'error',
                completed_at = COALESCE(duplicate.completed_at, CURRENT_TIMESTAMP),
                error_message = COALESCE(
                    duplicate.error_message,
                    'Superseded duplicate active run during migration 016'
                )
            FROM ranked
            WHERE duplicate.id = ranked.id
              AND ranked.position > 1
            """
        )
    )
    op.create_index(
        INDEX_NAME,
        "billing_job_run",
        ["billing_job_id", "billing_period_start", "billing_period_end"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Remove active-run ownership enforcement."""
    op.drop_index(INDEX_NAME, table_name="billing_job_run")
