"""Data-migration tests for logical snapshot and backup pricing."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

PRODUCTS = ("volume.snapshot.size", "volume.backup.size")


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "010_snapshot_backup_pricing.py"
    spec = importlib.util.spec_from_file_location("migration_010", path)
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


def _row(product: str, price: str = "1.73") -> dict:
    return {
        "resource_type": product,
        "unit_price": Decimal(price),
        "unit": "GB-month",
        "metadata_field": None,
        "metadata_value": None,
    }


def _setup(monkeypatch):
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    resource_price = _price_table(metadata)
    metadata.create_all(engine)
    connection = engine.connect()
    transaction = connection.begin()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    return migration, resource_price, connection, transaction


def _base_rows(connection, resource_price):
    return connection.execute(
        sa.select(
            resource_price.c.id,
            resource_price.c.resource_type,
            resource_price.c.unit_price,
            resource_price.c.unit,
        ).order_by(resource_price.c.resource_type, resource_price.c.id)
    ).all()


def test_upgrade_inserts_fresh_base_prices(monkeypatch) -> None:
    migration, table, connection, transaction = _setup(monkeypatch)
    try:
        migration.upgrade()

        assert [(row[1], row[2], row[3]) for row in _base_rows(connection, table)] == [
            ("volume.backup.size", Decimal("1.73"), "GB-month"),
            ("volume.snapshot.size", Decimal("1.73"), "GB-month"),
        ]
    finally:
        transaction.rollback()
        connection.close()


def test_upgrade_preserves_custom_existing_price(monkeypatch) -> None:
    migration, table, connection, transaction = _setup(monkeypatch)
    try:
        connection.execute(table.insert(), [_row("volume.snapshot.size", "2.45")])

        migration.upgrade()

        prices = {row[1]: row[2] for row in _base_rows(connection, table)}
        assert prices == {
            "volume.backup.size": Decimal("1.73"),
            "volume.snapshot.size": Decimal("2.45"),
        }
    finally:
        transaction.rollback()
        connection.close()


def test_upgrade_converges_duplicates_to_lowest_id(monkeypatch) -> None:
    migration, table, connection, transaction = _setup(monkeypatch)
    try:
        connection.execute(
            table.insert(),
            [
                _row("volume.snapshot.size", "2.45"),
                _row("volume.snapshot.size", "9.99"),
                _row("volume.backup.size", "3.21"),
                _row("volume.backup.size", "8.88"),
            ],
        )

        migration.upgrade()

        rows = _base_rows(connection, table)
        assert [(row[1], row[2]) for row in rows] == [
            ("volume.backup.size", Decimal("3.21")),
            ("volume.snapshot.size", Decimal("2.45")),
        ]
    finally:
        transaction.rollback()
        connection.close()


def test_upgrade_is_idempotent(monkeypatch) -> None:
    migration, table, connection, transaction = _setup(monkeypatch)
    try:
        migration.upgrade()
        first_rows = _base_rows(connection, table)

        migration.upgrade()

        assert _base_rows(connection, table) == first_rows
    finally:
        transaction.rollback()
        connection.close()


def test_downgrade_removes_new_products(monkeypatch) -> None:
    migration, table, connection, transaction = _setup(monkeypatch)
    try:
        connection.execute(
            table.insert(),
            [
                _row("volume.snapshot.size", "2.45"),
                {
                    **_row("volume.backup.size"),
                    "metadata_field": "policy",
                    "metadata_value": "daily",
                },
                _row("volume.size"),
            ],
        )

        migration.downgrade()

        assert [row[1] for row in _base_rows(connection, table)] == ["volume.size"]
    finally:
        transaction.rollback()
        connection.close()
