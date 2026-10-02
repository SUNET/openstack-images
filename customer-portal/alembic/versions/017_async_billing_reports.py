"""Add durable asynchronous billing reports and query checkpoints.

Revision ID: 017
Revises: 016
"""

import sqlalchemy as sa

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_report",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("billing_job_run_id", sa.Integer(), nullable=True),
        sa.Column("requested_by_sub", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("billing_period_start", sa.DateTime(), nullable=False),
        sa.Column("billing_period_end", sa.DateTime(), nullable=False),
        sa.Column("contract_numbers_json", sa.Text(), nullable=False),
        sa.Column("input_snapshot_json", sa.Text(), nullable=True),
        sa.Column("filename_template", sa.String(length=255), nullable=False),
        sa.Column("per_contract", sa.Boolean(), nullable=False),
        sa.Column("delivery_method", sa.String(length=50), nullable=True),
        sa.Column("delivery_config", sa.Text(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("result_filename", sa.String(length=255), nullable=True),
        sa.Column("result_media_type", sa.String(length=128), nullable=True),
        sa.Column("result_content", sa.LargeBinary(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_size", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'expired')",
            name="ck_billing_report_status",
        ),
        sa.CheckConstraint(
            "(delivery_method IS NULL AND delivery_config IS NULL) OR "
            "(delivery_method IN ('webdav', 'email') AND "
            "(delivery_config IS NOT NULL OR status IN ('succeeded', 'expired')))",
            name="ck_billing_report_delivery",
        ),
        sa.ForeignKeyConstraint(
            ["billing_job_run_id"],
            ["billing_job_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "billing_job_run_id", name="uq_billing_report_job_run"
        ),
    )
    op.create_index(
        "ix_billing_report_queue", "billing_report", ["status", "created_at"]
    )
    op.create_index(
        "ix_billing_report_owner",
        "billing_report",
        ["requested_by_sub", "created_at"],
    )
    op.create_table(
        "billing_report_shard",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("metric", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["billing_report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_id",
            "metric",
            "project_id",
            "window_start",
            "window_end",
            name="uq_billing_report_shard_window",
        ),
    )
    op.create_index(
        "ix_billing_report_shard_pending",
        "billing_report_shard",
        ["report_id", "status", "id"],
    )
    op.create_table(
        "billing_report_output",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'sent')",
            name="ck_billing_report_output_status",
        ),
        sa.CheckConstraint("size >= 0", name="ck_billing_report_output_size"),
        sa.CheckConstraint(
            "(status = 'sent' AND delivered_at IS NOT NULL) OR "
            "(status IN ('pending', 'ready') AND delivered_at IS NULL)",
            name="ck_billing_report_output_delivery",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["billing_report.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_id", "filename", name="uq_billing_report_output_filename"
        ),
    )
    op.create_index(
        "ix_billing_report_output_pending",
        "billing_report_output",
        ["report_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_report_output_pending", table_name="billing_report_output"
    )
    op.drop_table("billing_report_output")
    op.drop_index("ix_billing_report_shard_pending", table_name="billing_report_shard")
    op.drop_table("billing_report_shard")
    op.drop_index("ix_billing_report_owner", table_name="billing_report")
    op.drop_index("ix_billing_report_queue", table_name="billing_report")
    op.drop_table("billing_report")
