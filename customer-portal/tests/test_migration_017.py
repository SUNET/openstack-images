"""Operation-contract tests for migration 017."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, call

from app.models import BillingReport, BillingReportOutput


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "017_async_billing_reports.py"
    )
    spec = importlib.util.spec_from_file_location("migration_017", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_report_queue_shards_and_outputs(monkeypatch) -> None:
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    assert operation.create_table.call_args_list[0].args[0] == "billing_report"
    assert operation.create_table.call_args_list[1].args[0] == (
        "billing_report_shard"
    )
    assert operation.create_table.call_args_list[2].args[0] == (
        "billing_report_output"
    )
    report_columns = {
        column.name: column
        for column in operation.create_table.call_args_list[0].args[1:]
        if hasattr(column, "name")
    }
    assert report_columns["input_snapshot_json"].nullable is True
    assert report_columns["billing_job_run_id"].nullable is True
    assert report_columns["delivery_method"].nullable is True
    assert report_columns["delivery_config"].nullable is True
    assert operation.create_index.call_args_list == [
        call(
            "ix_billing_report_queue",
            "billing_report",
            ["status", "created_at"],
        ),
        call(
            "ix_billing_report_owner",
            "billing_report",
            ["requested_by_sub", "created_at"],
        ),
        call(
            "ix_billing_report_shard_pending",
            "billing_report_shard",
            ["report_id", "status", "id"],
        ),
        call(
            "ix_billing_report_output_pending",
            "billing_report_output",
            ["report_id", "status", "id"],
        ),
    ]


def test_downgrade_removes_shards_before_reports(monkeypatch) -> None:
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.method_calls == [
        call.drop_index(
            "ix_billing_report_output_pending",
            table_name="billing_report_output",
        ),
        call.drop_table("billing_report_output"),
        call.drop_index(
            "ix_billing_report_shard_pending",
            table_name="billing_report_shard",
        ),
        call.drop_table("billing_report_shard"),
        call.drop_index("ix_billing_report_owner", table_name="billing_report"),
        call.drop_index("ix_billing_report_queue", table_name="billing_report"),
        call.drop_table("billing_report"),
    ]


def test_billing_report_model_exposes_nullable_input_snapshot() -> None:
    column = BillingReport.__table__.c.input_snapshot_json

    assert column.type.__class__.__name__ == "Text"
    assert column.nullable is True


def test_output_model_matches_migration_constraints() -> None:
    table = BillingReportOutput.__table__

    assert next(iter(table.c.report_id.foreign_keys)).ondelete == "CASCADE"
    assert table.c.content.nullable is False
    assert table.c.status.nullable is False
    assert table.c.delivered_at.nullable is True
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_billing_report_output_delivery",
        "ck_billing_report_output_size",
        "ck_billing_report_output_status",
        "uq_billing_report_output_filename",
    }
