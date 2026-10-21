"""Operation-contract tests for migration 018."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, call


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "018_cluster_decommissioning.py"
    )
    spec = importlib.util.spec_from_file_location("migration_018", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_preserved_decommissioning_state(monkeypatch) -> None:
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    assert [item.args[1].name for item in operation.add_column.call_args_list] == [
        "decommissioned_at",
        "decommissioned_by_sub",
        "decommission_note",
    ]


def test_downgrade_removes_decommissioning_state(monkeypatch) -> None:
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.method_calls == [
        call.drop_column("tenant_cluster", "decommission_note"),
        call.drop_column("tenant_cluster", "decommissioned_by_sub"),
        call.drop_column("tenant_cluster", "decommissioned_at"),
    ]
