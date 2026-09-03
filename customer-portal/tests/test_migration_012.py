"""Tests for migration 012 optional Argo CD alias metadata."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "012_argocd_alias.py"
    spec = importlib.util.spec_from_file_location("migration_012", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_nullable_alias_without_backfill(monkeypatch) -> None:
    migration = _load_migration()
    calls = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("backfill")),
    )

    migration.upgrade()

    assert len(calls) == 1
    table_name, column = calls[0][0]
    assert table_name == "tenant_cluster"
    assert column.name == "argocd_alias"
    assert isinstance(column.type, sa.String)
    assert column.type.length == 253
    assert column.nullable is True


def test_downgrade_drops_alias(monkeypatch) -> None:
    migration = _load_migration()
    calls = []
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    migration.downgrade()

    assert calls == [(("tenant_cluster", "argocd_alias"), {})]
