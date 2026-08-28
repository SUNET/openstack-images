#!/usr/bin/env python3
"""Monthly billing CSV export.

Queries CloudKitty for resource usage, applies prices from the portal
database (global defaults with per-contract overrides), applies
per-contract rebates, and outputs a semicolon-delimited CSV.

Output format (one row per resource type per project):
    ContractNumber;Project;ResourceType;Volume;Cost

Can be run standalone:
    python -m app.billing_export [--month 2026-03] [--output billing.csv]
"""

import argparse
import csv
import io
import logging
import sys
from datetime import datetime, timedelta
from decimal import Decimal

import openstack
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import sessionmaker

from app.models import Contract, ContractPriceOverride, ContractRebate, ResourcePrice

logger = logging.getLogger(__name__)

# Mapping from CloudKitty metric names to billing resource type labels
METRIC_LABELS = {
    "instance": "Compute",
    "volume.size": "Storage",
    "radosgw.objects.size": "S3 Storage",
    "image.size": "Image Storage",
    "ip.floating": "Floating IP",
    "network.incoming.bytes.rate": "Network In",
    "network.outgoing.bytes.rate": "Network Out",
}

# Default units per metric (used in CSV output)
METRIC_UNITS = {
    "instance": "timmar",
    "volume.size": "Gbyte",
    "radosgw.objects.size": "Gbyte",
    "image.size": "Mbyte",
    "ip.floating": "st",
    "network.incoming.bytes.rate": "MB",
    "network.outgoing.bytes.rate": "MB",
}

CONTRACT_TAG_PREFIX = "contract:"


def get_billing_period(month_str: str | None = None) -> tuple[datetime, datetime]:
    """Return (start, end) for the billing period."""
    if month_str:
        start = datetime.strptime(month_str, "%Y-%m").replace(day=1)
    else:
        today = datetime.utcnow().replace(day=1)
        start = (today - timedelta(days=1)).replace(day=1)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


def get_project_contracts(conn: openstack.connection.Connection) -> dict[str, tuple[str, str]]:
    """Build project_id -> (project_name, contract_number) mapping."""
    project_map = {}
    for project in conn.identity.projects():
        contract_tags = sorted(
            tag for tag in (project.tags or []) if tag.startswith(CONTRACT_TAG_PREFIX)
        )
        if len(contract_tags) > 1:
            raise RuntimeError(
                f"Project {project.name} has multiple contract tags: {contract_tags}"
            )
        if contract_tags:
            contract_number = contract_tags[0][len(CONTRACT_TAG_PREFIX) :]
            if not contract_number:
                raise RuntimeError(f"Project {project.name} has an empty contract tag")
            project_map[project.id] = (project.name, contract_number)
    return project_map


def load_prices(db_session: SyncSession) -> dict[str, Decimal]:
    """Load global default prices keyed by resource_type."""
    result = db_session.execute(select(ResourcePrice))
    return {p.resource_type: p.unit_price for p in result.scalars()}


def load_contract_overrides(db_session: SyncSession) -> dict[int, dict[str, Decimal]]:
    """Load per-contract price overrides: contract_id -> {resource_type: price}."""
    result = db_session.execute(select(ContractPriceOverride))
    overrides: dict[int, dict[str, Decimal]] = {}
    for o in result.scalars():
        overrides.setdefault(o.contract_id, {})[o.resource_type] = o.unit_price
    return overrides


def load_rebates(db_session: SyncSession) -> dict[int, Decimal]:
    """Load per-contract rebates: contract_id -> rebate_percent."""
    result = db_session.execute(select(ContractRebate))
    return {r.contract_id: r.rebate_percent for r in result.scalars()}


def load_contract_ids(db_session: SyncSession) -> dict[str, int]:
    """Load contract_number -> contract_id mapping."""
    result = db_session.execute(select(Contract))
    return {c.contract_number: c.id for c in result.scalars()}


def query_cloudkitty_summary(
    conn: openstack.connection.Connection,
    begin: datetime,
    end: datetime,
) -> list[dict]:
    """Query CloudKitty v2 summary API for usage data."""
    rating_proxy = conn.rating
    summaries = []

    for metric_name in METRIC_LABELS:
        try:
            result = rating_proxy.get(
                "/v2/summary",
                params={
                    "begin": begin.isoformat(),
                    "end": end.isoformat(),
                    "groupby": "project_id,type",
                    "filters": f"type:{metric_name}",
                },
            )
            if result.status_code == 200:
                data = result.json()
                for entry in data.get("results", []):
                    summaries.append(
                        {
                            "project_id": entry.get("project_id", ""),
                            "metric": entry.get("type", metric_name),
                            "quantity": entry.get("qty", 0),
                        }
                    )
        except Exception:
            logger.exception("Failed to query CloudKitty for metric %s", metric_name)

    return summaries


def generate_csv(
    project_contracts: dict[str, tuple[str, str]],
    summaries: list[dict],
    global_prices: dict[str, Decimal],
    contract_overrides: dict[int, dict[str, Decimal]],
    rebates: dict[int, Decimal],
    contract_ids: dict[str, int],
    delimiter: str = ";",
) -> str:
    """Generate the billing CSV with prices and rebates applied."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)

    for entry in summaries:
        project_id = entry["project_id"]
        if project_id not in project_contracts:
            continue

        project_name, contract_number = project_contracts[project_id]
        metric = entry["metric"]
        label = METRIC_LABELS.get(metric)
        if not label:
            continue

        quantity = entry["quantity"]
        unit = METRIC_UNITS.get(metric, "")

        # Look up price: contract override > global default
        contract_id = contract_ids.get(contract_number)
        unit_price = Decimal(0)
        if contract_id and contract_id in contract_overrides:
            unit_price = contract_overrides[contract_id].get(label, Decimal(0))
        if unit_price == 0:
            unit_price = global_prices.get(label, Decimal(0))

        cost = Decimal(str(quantity)) * unit_price

        # Apply rebate
        if contract_id and contract_id in rebates:
            rebate_pct = rebates[contract_id]
            cost = cost * (1 - rebate_pct / 100)

        cost_int = round(cost)
        volume = f"{quantity:.2f} {unit}".replace(".", ",")

        writer.writerow([contract_number, project_name, label, volume, cost_int])

    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate monthly billing CSV")
    parser.add_argument("--month", help="Billing month YYYY-MM (default: previous month)")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    parser.add_argument("--cloud", default="openstack", help="OpenStack cloud name")
    parser.add_argument("--delimiter", default=";", help="CSV delimiter (default: ;)")
    parser.add_argument(
        "--database-url", default=None, help="Database URL (default: from DATABASE_URL env)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    import os

    db_url = args.database_url or os.environ.get(
        "DATABASE_URL", "postgresql://portal:portal@localhost:5432/portal"
    )
    # Ensure sync driver
    db_url = db_url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine)
    db_session = SessionLocal()

    begin, end = get_billing_period(args.month)
    logger.info("Billing period: %s to %s", begin.date(), end.date())

    # Load pricing from database
    global_prices = load_prices(db_session)
    contract_overrides = load_contract_overrides(db_session)
    rebates = load_rebates(db_session)
    contract_ids = load_contract_ids(db_session)
    logger.info(
        "Loaded %d global prices, %d contracts with overrides, %d with rebates",
        len(global_prices),
        len(contract_overrides),
        len(rebates),
    )

    # Connect to OpenStack
    conn = openstack.connect(cloud=args.cloud)

    logger.info("Fetching project contract mappings...")
    project_contracts = get_project_contracts(conn)
    logger.info("Found %d projects with contract numbers", len(project_contracts))

    logger.info("Querying CloudKitty for usage data...")
    summaries = query_cloudkitty_summary(conn, begin, end)
    logger.info("Got %d summary entries", len(summaries))

    csv_output = generate_csv(
        project_contracts,
        summaries,
        global_prices,
        contract_overrides,
        rebates,
        contract_ids,
        args.delimiter,
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(csv_output)
        logger.info("Written to %s", args.output)
    else:
        sys.stdout.write(csv_output)

    db_session.close()


if __name__ == "__main__":
    main()
