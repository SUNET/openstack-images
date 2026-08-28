"""Data-migration tests for canonical Cinder volume-type pricing."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa


def _load_migration():
    path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "009_canonical_cinder_volume_type.py"
    )
    spec = importlib.util.spec_from_file_location("migration_009", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _price_table(metadata):
    return sa.Table(
        "resource_price",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("metadata_field", sa.String(100)),
        sa.Column("metadata_value", sa.String(255)),
        sa.UniqueConstraint(
            "resource_type",
            "metadata_field",
            "metadata_value",
            name="uq_resource_price_type_meta",
        ),
    )


def _row(volume_type: str, price: str) -> dict:
    return {
        "resource_type": "volume.size",
        "unit_price": Decimal(price),
        "unit": "GB-month",
        "metadata_field": "volume_type",
        "metadata_value": volume_type,
    }


@pytest.mark.parametrize(
    ("initial", "expected_price"),
    [
        ([_row("large", "2.34"), _row("fast", "5.18")], Decimal("2.34")),
        (
            [
                _row("large", "1.73"),
                _row("fast", "5.18"),
                _row("rbd1", "2.50"),
            ],
            Decimal("2.50"),
        ),
        ([], Decimal("1.73")),
    ],
)
def test_upgrade_converges_to_one_rbd1_price(monkeypatch, initial, expected_price) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    resource_price = _price_table(metadata)
    metadata.create_all(engine)

    with engine.begin() as connection:
        if initial:
            connection.execute(resource_price.insert(), initial)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        rows = connection.execute(
            sa.select(resource_price.c.metadata_value, resource_price.c.unit_price)
        ).all()
        assert rows == [("rbd1", expected_price)]


def test_downgrade_restores_legacy_seed_names(monkeypatch) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    resource_price = _price_table(metadata)
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(resource_price.insert(), [_row("large", "2.34")])
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        migration.downgrade()

        rows = connection.execute(
            sa.select(resource_price.c.metadata_value, resource_price.c.unit_price).order_by(
                resource_price.c.metadata_value
            )
        ).all()
        assert rows == [("fast", Decimal("5.18")), ("large", Decimal("2.34"))]
