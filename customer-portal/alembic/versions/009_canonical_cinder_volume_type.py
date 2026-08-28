"""Use the deployed Cinder volume type name for block-storage pricing.

Revision ID: 009
Create Date: 2026-08-28
"""

from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def _price_table():
    return sa.table(
        "resource_price",
        sa.column("id", sa.Integer),
        sa.column("resource_type", sa.String),
        sa.column("unit_price", sa.Numeric(12, 2)),
        sa.column("unit", sa.String),
        sa.column("metadata_field", sa.String),
        sa.column("metadata_value", sa.String),
    )


def _volume_price(connection, resource_price, volume_type: str):
    return (
        connection.execute(
            sa.select(resource_price).where(
                resource_price.c.resource_type == "volume.size",
                resource_price.c.metadata_field == "volume_type",
                resource_price.c.metadata_value == volume_type,
            )
        )
        .mappings()
        .one_or_none()
    )


def upgrade() -> None:
    connection = op.get_bind()
    resource_price = _price_table()
    legacy_large = _volume_price(connection, resource_price, "large")
    canonical = _volume_price(connection, resource_price, "rbd1")

    if canonical is None and legacy_large is not None:
        connection.execute(
            resource_price.update()
            .where(resource_price.c.id == legacy_large["id"])
            .values(metadata_value="rbd1")
        )
    elif canonical is None:
        connection.execute(
            resource_price.insert().values(
                resource_type="volume.size",
                unit_price=Decimal("1.73"),
                unit="GB-month",
                metadata_field="volume_type",
                metadata_value="rbd1",
            )
        )
    elif legacy_large is not None:
        connection.execute(
            resource_price.delete().where(resource_price.c.id == legacy_large["id"])
        )

    connection.execute(
        resource_price.delete().where(
            resource_price.c.resource_type == "volume.size",
            resource_price.c.metadata_field == "volume_type",
            resource_price.c.metadata_value == "fast",
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    resource_price = _price_table()
    canonical = _volume_price(connection, resource_price, "rbd1")
    legacy_large = _volume_price(connection, resource_price, "large")

    if legacy_large is None and canonical is not None:
        connection.execute(
            resource_price.update()
            .where(resource_price.c.id == canonical["id"])
            .values(metadata_value="large")
        )
    elif canonical is not None:
        connection.execute(resource_price.delete().where(resource_price.c.id == canonical["id"]))

    if _volume_price(connection, resource_price, "fast") is None:
        connection.execute(
            resource_price.insert().values(
                resource_type="volume.size",
                unit_price=Decimal("5.18"),
                unit="GB-month",
                metadata_field="volume_type",
                metadata_value="fast",
            )
        )
