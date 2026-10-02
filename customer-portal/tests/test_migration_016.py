"""Migration tests for atomic billing-run ownership."""

import importlib.util
from pathlib import Path


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "016_atomic_billing_runs.py"
    spec = importlib.util.spec_from_file_location("migration_016", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_repairs_duplicates_and_creates_partial_unique_index(monkeypatch) -> None:
    migration = _load_migration()
    operations = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: operations.append(statement))
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: operations.append((args, kwargs)),
    )

    migration.upgrade()

    repair_sql = str(operations[0])
    assert "row_number() OVER" in repair_sql
    assert "WHERE status = 'running'" in repair_sql
    assert "SET status = 'error'" in repair_sql
    args, kwargs = operations[1]
    assert args[:2] == (migration.INDEX_NAME, "billing_job_run")
    assert kwargs["unique"] is True
    assert str(kwargs["postgresql_where"]) == "status = 'running'"


def test_downgrade_drops_active_run_index(monkeypatch) -> None:
    migration = _load_migration()
    dropped = []
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )

    migration.downgrade()

    assert dropped == [((migration.INDEX_NAME,), {"table_name": "billing_job_run"})]
