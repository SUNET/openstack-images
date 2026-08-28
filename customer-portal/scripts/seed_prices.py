"""Seed compute + storage resource_price rows from the SUNET VDC pricelist.

Idempotent: upserts by (resource_type, metadata_field, metadata_value).
Safe to re-run. Mirrors Sunet-VDC-prislista_2603.pdf. Kubernetes/cluster
fees are intentionally left untouched (managed via migration 008).

Run against the running pod (reads/writes the live DB):

    kubectl --context openstack-prod -n customer-portal \\
      exec -i deploy/customer-portal -- python3 - \\
      < customer-portal/scripts/seed_prices.py

Set DRY_RUN = True to preview without committing.

Pricing model notes:
 * VMs are listed per month but the engine bills uptime hours, so each
   flavor's hourly rate = monthly / 730 (avg hours/month).
 * Block/object storage bills per GB-month (engine change in
   billing_runner.py computes time-weighted GB over the period).
"""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import ResourcePrice

DRY_RUN = False

HOURS_PER_MONTH = Decimal(730)
CENT = Decimal("0.01")

# b2 flavors: flavor_name -> monthly price (SEK). No l2 flavors in cluster.
INSTANCE_MONTHLY = {
    "b2.c1r2": 275,
    "b2.c1r4": 435,
    "b2.c2r4": 550,
    "b2.c2r8": 865,
    "b2.c4r8": 1095,
    "b2.c4r16": 1725,
    "b2.c8r16": 2185,
    "b2.c8r32": 3450,
    "b2.c16r32": 4370,
    "b2.c16r64": 6900,
}


def _hourly(monthly: int) -> Decimal:
    return (Decimal(monthly) / HOURS_PER_MONTH).quantize(CENT, ROUND_HALF_UP)


def _desired_prices() -> list[dict]:
    rows: list[dict] = [
        # --- Block storage (per GB-month) ---
        {
            "resource_type": "volume.size",
            "unit_price": Decimal("1.73"),
            "unit": "GB-month",
            "field": "volume_type",
            "value": "rbd1",
        },
        # --- Object storage S3 (per GB-month, no binding) ---
        {
            "resource_type": "radosgw.objects.size",
            "unit_price": Decimal("0.36"),
            "unit": "GB-month",
            "field": None,
            "value": None,
        },
    ]
    # --- Virtual machines (per uptime hour) ---
    for name, monthly in INSTANCE_MONTHLY.items():
        rows.append(
            {
                "resource_type": "instance",
                "unit_price": _hourly(monthly),
                "unit": "hour",
                "field": "flavor_name",
                "value": name,
            }
        )
    return rows


def _upsert(db, row: dict) -> str:
    q = select(ResourcePrice).where(ResourcePrice.resource_type == row["resource_type"])
    if row["field"] is None:
        q = q.where(ResourcePrice.metadata_field.is_(None))
    else:
        q = q.where(
            ResourcePrice.metadata_field == row["field"],
            ResourcePrice.metadata_value == row["value"],
        )
    existing = db.execute(q).scalar_one_or_none()
    label = f"{row['resource_type']} {row['field']}={row['value']}"
    if existing is None:
        db.add(
            ResourcePrice(
                resource_type=row["resource_type"],
                unit_price=row["unit_price"],
                unit=row["unit"],
                metadata_field=row["field"],
                metadata_value=row["value"],
            )
        )
        return f"INSERT {label} = {row['unit_price']} {row['unit']}"
    if existing.unit_price != row["unit_price"] or existing.unit != row["unit"]:
        old = f"{existing.unit_price} {existing.unit}"
        existing.unit_price = row["unit_price"]
        existing.unit = row["unit"]
        return f"UPDATE {label} = {old} -> {row['unit_price']} {row['unit']}"
    return f"ok     {label} = {row['unit_price']} {row['unit']}"


def main() -> None:
    url = get_settings().database_url.replace("+asyncpg", "")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    db = sessionmaker(bind=create_engine(url))()
    try:
        for row in _desired_prices():
            print(_upsert(db, row))

        if DRY_RUN:
            db.rollback()
            print("\nDRY_RUN: rolled back, no changes committed.")
        else:
            db.commit()
            print("\nCommitted.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
