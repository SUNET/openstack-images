"""Seed logical snapshot and backup storage pricing.

Revision ID: 010
Create Date: 2026-08-31
"""

from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

PRODUCTS = ("volume.snapshot.size", "volume.backup.size")


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


def upgrade() -> None:
    connection = op.get_bind()
    resource_price = _price_table()

    for product in PRODUCTS:
        existing = (
            connection.execute(
                sa.select(resource_price.c.id)
                .where(
                    resource_price.c.resource_type == product,
                    resource_price.c.metadata_field.is_(None),
                    resource_price.c.metadata_value.is_(None),
                )
                .order_by(resource_price.c.id)
            )
            .scalars()
            .all()
        )
        if not existing:
            connection.execute(
                resource_price.insert().values(
                    resource_type=product,
                    unit_price=Decimal("1.73"),
                    unit="GB-month",
                    metadata_field=None,
                    metadata_value=None,
                )
            )
        elif len(existing) > 1:
            connection.execute(
                resource_price.delete().where(resource_price.c.id.in_(existing[1:]))
            )


def downgrade() -> None:
    connection = op.get_bind()
    resource_price = _price_table()
    connection.execute(resource_price.delete().where(resource_price.c.resource_type.in_(PRODUCTS)))
