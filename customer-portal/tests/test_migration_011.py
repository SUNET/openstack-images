"""Tests for migration 011 planned tenant-cluster connection fields."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa


def _load_migration():
    path = (
        Path(__file__).parent.parent
        / "alembic"
        / "versions"
        / "011_planned_tenant_clusters.py"
    )
    spec = importlib.util.spec_from_file_location("migration_011", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_alter_calls(monkeypatch, migration):
    calls = []
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def test_upgrade_makes_cluster_connection_fields_nullable(monkeypatch) -> None:
    migration = _load_migration()
    calls = _record_alter_calls(monkeypatch, migration)

    migration.upgrade()

    assert [(args[:2], kwargs["nullable"]) for args, kwargs in calls] == [
        (("tenant_cluster", "api_url"), True),
        (("tenant_cluster", "ca_bundle"), True),
    ]
    assert isinstance(calls[0][1]["existing_type"], sa.String)
    assert isinstance(calls[1][1]["existing_type"], sa.Text)


def test_downgrade_restores_non_nullable_connection_fields(monkeypatch) -> None:
    migration = _load_migration()
    calls = _record_alter_calls(monkeypatch, migration)
    statements = []
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: statements.append(statement),
    )

    migration.downgrade()

    assert len(statements) == 2
    assert "api_url IS NULL" in statements[0]
    assert "ca_bundle IS NULL" in statements[1]
    assert [(args[:2], kwargs["nullable"]) for args, kwargs in calls] == [
        (("tenant_cluster", "ca_bundle"), False),
        (("tenant_cluster", "api_url"), False),
    ]
