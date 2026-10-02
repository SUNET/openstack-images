"""Billing job execution: CSV generation, delivery, scheduling."""

import asyncio
import csv
import io
import json
import logging
import re
import smtplib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from uuid import uuid4

import httpx
import openstack
from croniter import croniter
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import sessionmaker

from app.billing_report_state import requeue_failed_report
from app.config import get_settings
from app.crypto import decrypt_value
from app.models import (
    BillingJob,
    BillingJobContract,
    BillingJobRun,
    BillingReport,
    ClusterAddon,
    ClusterRequest,
    Contract,
    ContractAccess,
    ContractPriceOverride,
    ContractRebate,
    Customer,
    ResourcePrice,
    TenantCluster,
)

logger = logging.getLogger(__name__)

CONTRACT_TAG_PREFIX = "contract:"
BILLING_GRANULARITY_SECONDS = 3600
MAX_GNOCCHI_GROUPS_PER_PROJECT = 10000
MAX_GNOCCHI_MEASURES_PER_GROUP = 20000
MAX_GNOCCHI_RESPONSE_BYTES = 10 * 1024 * 1024
BILLING_CSV_HEADER = [
    "# Customer",
    "ContractNumber",
    "Project",
    "ResourceType",
    "Quantity",
    "Unit",
    "Cost",
]
UTF8_BOM = "\ufeff"
BILLING_INPUT_SNAPSHOT_VERSION = 1
SYNTHETIC_RESOURCE_TYPES = {
    "cluster_management_fee",
    "cluster_management_fee_increment",
    "cluster_setup_fee",
    "cluster_addon_fee",
}


def _utc_now() -> datetime:
    """Return naive UTC for the existing timestamp-without-time-zone schema."""
    return datetime.now(UTC).replace(tzinfo=None)

# The `instance` product is billed from CPU sample presence because Ceilometer
# does not emit a continuously sampled metric named `instance`.
GNOCCHI_PRODUCT_REGISTRY = {
    "instance": {
        "resource_type": "instance",
        "source_metric": "cpu",
        "metadata_fields": {"flavor_name"},
        "aggregation": "resource_hours",
        "unit": "hour",
        "size_gb_scale": None,
    },
    "volume.size": {
        "resource_type": "volume",
        "source_metric": "volume.size",
        "metadata_fields": {"volume_type"},
        "aggregation": "additive_size",
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "volume.snapshot.size": {
        "resource_type": "volume",
        "source_metric": "volume.snapshot.size",
        "metadata_fields": set(),
        "aggregation": "additive_size",
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "volume.backup.size": {
        "resource_type": "volume",
        "source_metric": "volume.backup.size",
        "metadata_fields": set(),
        "aggregation": "additive_size",
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "radosgw.objects.size": {
        "resource_type": "ceph_account",
        "source_metric": "radosgw.objects.size",
        "metadata_fields": set(),
        "aggregation": "additive_size",
        "unit": "GB-month",
        "size_gb_scale": Decimal(1) / Decimal(10**9),
    },
}
GNOCCHI_METRIC_SOURCES = {
    product: (config["resource_type"], config["source_metric"])
    for product, config in GNOCCHI_PRODUCT_REGISTRY.items()
}
GNOCCHI_METRIC_METADATA_FIELDS = {
    product: set(config["metadata_fields"]) for product, config in GNOCCHI_PRODUCT_REGISTRY.items()
}

class BillingGenerationError(RuntimeError):
    """Raised when a billing report cannot be generated completely."""


class GnocchiShardTooLarge(BillingGenerationError):
    """Raised when a bounded query must be divided into smaller windows."""


class GnocchiQueryTimeout(BillingGenerationError):
    """Raised when a bounded query must be divided or retried."""


def _get_cinder_volume_type_names(conn) -> dict[str, str]:
    """Return active Cinder volume type IDs mapped to their canonical names."""
    try:
        result: dict[str, str] = {}
        for volume_type in conn.block_storage.types():
            type_id = getattr(volume_type, "id", None)
            name = getattr(volume_type, "name", None)
            if not isinstance(type_id, str) or not type_id:
                raise BillingGenerationError("Cinder returned a volume type without an ID")
            if not isinstance(name, str) or not name:
                raise BillingGenerationError(
                    f"Cinder returned volume type {type_id} without a name"
                )
            if type_id in result and result[type_id] != name:
                raise BillingGenerationError(
                    f"Cinder returned conflicting names for volume type {type_id}"
                )
            result[type_id] = name
        if not result:
            raise BillingGenerationError("Cinder returned no active volume types")
        return result
    except BillingGenerationError:
        raise
    except Exception as exc:
        raise BillingGenerationError("Failed to list Cinder volume types") from exc


def _resolve_cinder_volume_type(value: str, type_names: dict[str, str]) -> str:
    """Resolve Gnocchi's Cinder type ID while accepting canonical names."""
    if value in type_names:
        return type_names[value]
    if value in type_names.values():
        return value
    raise BillingGenerationError(f"Cinder volume type {value} is unknown or no longer active")


def _canonicalize_volume_usage(usage: list[dict], type_names: dict[str, str]) -> list[dict]:
    """Resolve Cinder type IDs and roll equivalent historical groups together."""
    canonical: dict[tuple, dict] = {}
    for entry in usage:
        metadata = dict(entry.get("metadata", {}))
        raw_volume_type = metadata.get("volume_type")
        if not isinstance(raw_volume_type, str) or not raw_volume_type:
            raise BillingGenerationError("Volume usage is missing volume_type")
        metadata["volume_type"] = _resolve_cinder_volume_type(raw_volume_type, type_names)

        key = (
            entry["project_id"],
            tuple((field, metadata[field]) for field in sorted(metadata)),
        )
        result = canonical.setdefault(
            key,
            {
                "project_id": entry["project_id"],
                "metric": entry["metric"],
                "metadata": metadata,
                "hours": Decimal(0),
                "size_months": Decimal(0),
            },
        )
        result["hours"] += Decimal(str(entry["hours"]))
        result["size_months"] += Decimal(str(entry["size_months"]))
    return list(canonical.values())


def discover_gnocchi_metrics(cloud_name: str = "openstack") -> list[dict]:
    """Discover available metric/resource types and their metadata values from Gnocchi.

    Returns a list of dicts:
      {metric_type, resource_type, unit, metadata_fields: [{field, values: []}]}
    """
    import httpx

    gnocchi = "http://gnocchi-api.openstack.svc.cluster.local:8041"

    try:
        conn = openstack.connect(cloud=cloud_name)
        token = conn.auth_token
        volume_type_names = None

        results = []
        for metric_type, info in GNOCCHI_PRODUCT_REGISTRY.items():
            entry = {
                "metric_type": metric_type,
                "resource_type": info["resource_type"],
                "unit": info["unit"],
                "metadata_fields": [],
            }

            # For each metadata field, query Gnocchi for distinct values
            for field in sorted(info["metadata_fields"]):
                values = set()
                try:
                    resp = httpx.get(
                        f"{gnocchi}/v1/resource/{info['resource_type']}",
                        headers={"X-Auth-Token": token},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        for resource in resp.json():
                            val = resource.get(field)
                            if val and field == "volume_type":
                                if volume_type_names is None:
                                    volume_type_names = _get_cinder_volume_type_names(conn)
                                val = _resolve_cinder_volume_type(val, volume_type_names)
                            if val:
                                values.add(val)
                except Exception:
                    logger.exception(
                        "Failed to query Gnocchi for %s metadata", info["resource_type"]
                    )

                entry["metadata_fields"].append(
                    {
                        "field": field,
                        "values": sorted(values),
                    }
                )

            results.append(entry)

        return results
    except Exception:
        logger.exception("Failed to discover Gnocchi metrics")
        return []


# --- Billing period ---


def get_billing_period(
    year: int | None = None, month: int | None = None
) -> tuple[datetime, datetime]:
    """Return (start, end) for a billing period. Defaults to previous month."""
    if year and month:
        start = datetime(year, month, 1)
    else:
        now = datetime.now(UTC)
        this_month = datetime(now.year, now.month, 1)
        start = (this_month - timedelta(days=1)).replace(day=1)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


# --- Contract resolution ---


def resolve_contract_numbers(
    sync_session: SyncSession, job: BillingJob, admin_users: list[str]
) -> list[str]:
    """Resolve which contract numbers a job should bill for."""
    if job.all_contracts:
        if job.owner_sub in admin_users:
            result = sync_session.execute(select(Contract.contract_number))
        else:
            result = sync_session.execute(
                select(Contract.contract_number)
                .join(ContractAccess)
                .where(ContractAccess.user_sub == job.owner_sub)
            )
        return [r[0] for r in result]
    else:
        result = sync_session.execute(
            select(Contract.contract_number)
            .join(BillingJobContract, BillingJobContract.contract_id == Contract.id)
            .where(BillingJobContract.billing_job_id == job.id)
        )
        return [r[0] for r in result]


# --- CSV generation (sync, runs in thread pool) ---


def _get_project_contracts(conn: openstack.connection.Connection) -> dict[str, tuple[str, str]]:
    """Build project_id -> (project_name, contract_number) mapping."""
    project_map = {}
    for project in conn.identity.projects():
        contract_tags = sorted(
            tag for tag in (project.tags or []) if tag.startswith(CONTRACT_TAG_PREFIX)
        )
        if len(contract_tags) > 1:
            raise BillingGenerationError(
                f"Project {project.name} has multiple contract tags: {contract_tags}"
            )
        if contract_tags:
            contract_number = contract_tags[0][len(CONTRACT_TAG_PREFIX) :]
            if not contract_number:
                raise BillingGenerationError(f"Project {project.name} has an empty contract tag")
            project_map[project.id] = (project.name, contract_number)
    return project_map


def _load_prices(sync_session: SyncSession) -> list[ResourcePrice]:
    """Load all resource prices, ordered so specific (metadata) prices come first."""
    result = sync_session.execute(
        select(ResourcePrice).order_by(
            ResourcePrice.resource_type,
            ResourcePrice.metadata_field.desc(),  # non-null first
            ResourcePrice.metadata_value,
            ResourcePrice.id,
        )
    )
    return list(result.scalars())


def _find_price(
    prices: list[ResourcePrice], metric: str, metadata: dict[str, str]
) -> ResourcePrice | None:
    """Find the most specific matching price for a metric + metadata combo.

    Specific (metadata_field+metadata_value match) takes priority over base (no metadata).
    """
    base_match = None
    for p in prices:
        if p.resource_type != metric:
            continue
        if p.metadata_field and p.metadata_value:
            # Specific price — check if metadata matches
            if metadata.get(p.metadata_field) == p.metadata_value:
                return p  # most specific, return immediately
        elif not p.metadata_field:
            base_match = p  # fallback
    return base_match


def _load_contract_overrides(sync_session: SyncSession) -> dict[int, dict[str, Decimal]]:
    result = sync_session.execute(select(ContractPriceOverride))
    overrides: dict[int, dict[str, Decimal]] = {}
    for o in result.scalars():
        overrides.setdefault(o.contract_id, {})[o.resource_type] = o.unit_price
    return overrides


def _load_rebates(sync_session: SyncSession) -> dict[int, Decimal]:
    result = sync_session.execute(select(ContractRebate))
    return {r.contract_id: r.rebate_percent for r in result.scalars()}


def _load_contract_ids(sync_session: SyncSession) -> dict[str, int]:
    result = sync_session.execute(select(Contract))
    return {c.contract_number: c.id for c in result.scalars()}


def _load_contract_customers(sync_session: SyncSession) -> dict[str, str]:
    """Load contract_number -> customer name mapping."""
    result = sync_session.execute(
        select(Contract.contract_number, Customer.name).join(
            Customer, Customer.id == Contract.customer_id
        )
    )
    return dict(result.all())


def _snapshot_datetime(value: datetime) -> str:
    """Serialize a database timestamp with an explicit UTC offset."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def serialize_billing_input_snapshot(snapshot: dict[str, Any]) -> str:
    """Return canonical JSON for an immutable billing input snapshot."""

    def encode(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return _snapshot_datetime(value)
        raise TypeError(f"Unsupported billing snapshot value: {type(value).__name__}")

    return json.dumps(
        snapshot,
        default=encode,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_billing_input_snapshot(snapshot_json: str) -> dict[str, Any]:
    """Load and reject snapshot formats this worker cannot interpret."""
    try:
        snapshot = json.loads(snapshot_json)
    except (TypeError, ValueError) as exc:
        raise BillingGenerationError("Invalid billing input snapshot") from exc
    if not isinstance(snapshot, dict) or snapshot.get("version") != BILLING_INPUT_SNAPSHOT_VERSION:
        raise BillingGenerationError("Unsupported billing input snapshot version")
    return snapshot


def _capture_synthetic_facts(
    sync_session: SyncSession,
    period_start: datetime,
    period_end: datetime,
    contract_id_to_number: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    """Capture all cluster facts that can produce lines in this period."""
    clusters = list(
        sync_session.execute(
            select(TenantCluster).where(
                TenantCluster.provisioned_at.is_not(None),
                TenantCluster.provisioned_at < period_end,
                TenantCluster.contract_id.in_(contract_id_to_number),
            )
        ).scalars()
    )
    resize_facts = []
    future_resize_deltas: dict[int, int] = {}
    resize_rows = sync_session.execute(
        select(ClusterRequest, TenantCluster)
        .join(TenantCluster, TenantCluster.id == ClusterRequest.cluster_id)
        .where(
            ClusterRequest.request_type == "resize",
            ClusterRequest.status == "applied",
            ClusterRequest.applied_at.is_not(None),
            ClusterRequest.applied_at >= period_start,
            TenantCluster.contract_id.in_(contract_id_to_number),
        )
    ).all()
    for request, cluster in sorted(resize_rows, key=lambda item: item[0].id):
        try:
            payload = json.loads(request.payload)
        except (TypeError, ValueError):
            if request.applied_at >= period_end:
                raise BillingGenerationError(
                    f"Cannot reconstruct period-end worker groups for cluster {cluster.slug}"
                )
            continue
        before = payload.get("before_worker_groups")
        target = payload.get("target_worker_groups")
        invalid = (
            isinstance(before, bool)
            or isinstance(target, bool)
            or not isinstance(before, int)
            or not isinstance(target, int)
            or target <= before
        )
        if invalid and request.applied_at >= period_end:
            raise BillingGenerationError(
                f"Cannot reconstruct period-end worker groups for cluster {cluster.slug}"
            )
        if invalid:
            continue
        delta = target - before
        if request.applied_at >= period_end:
            future_resize_deltas[cluster.id] = (
                future_resize_deltas.get(cluster.id, 0) + delta
            )
            continue
        resize_facts.append(
            {
                "applied_at": _snapshot_datetime(request.applied_at),
                "contract_number": contract_id_to_number[cluster.contract_id],
                "delta_worker_groups": delta,
                "slug": cluster.slug,
            }
        )

    cluster_facts = []
    for cluster in sorted(clusters, key=lambda item: item.id):
        period_worker_groups = cluster.worker_groups - future_resize_deltas.get(
            cluster.id, 0
        )
        if period_worker_groups <= 0:
            raise BillingGenerationError(
                f"Invalid reconstructed worker groups for cluster {cluster.slug}"
            )
        cluster_facts.append(
            {
                "contract_number": contract_id_to_number[cluster.contract_id],
                "initial_worker_groups": cluster.initial_worker_groups,
                "provisioned_at": _snapshot_datetime(cluster.provisioned_at),
                "slug": cluster.slug,
                "worker_groups": period_worker_groups,
            }
        )

    addon_rows = sync_session.execute(
        select(ClusterAddon, TenantCluster)
        .join(TenantCluster, TenantCluster.id == ClusterAddon.cluster_id)
        .where(
            ClusterAddon.enabled_at < period_end,
            (ClusterAddon.disabled_at.is_(None))
            | (ClusterAddon.disabled_at > period_start),
            TenantCluster.contract_id.in_(contract_id_to_number),
        )
    ).all()
    addon_facts = [
        {
            "addon_type": addon.addon_type,
            "contract_number": contract_id_to_number[cluster.contract_id],
            "disabled_at": (
                _snapshot_datetime(addon.disabled_at)
                if addon.disabled_at is not None
                else None
            ),
            "enabled_at": _snapshot_datetime(addon.enabled_at),
            "slug": cluster.slug,
        }
        for addon, cluster in sorted(addon_rows, key=lambda item: item[0].id)
    ]
    return {"addons": addon_facts, "clusters": cluster_facts, "resizes": resize_facts}


def capture_billing_input_snapshot(
    sync_session: SyncSession,
    conn,
    contract_numbers: list[str],
    period_start: datetime,
    period_end: datetime,
    *,
    filename_template: str = "billing-{year}-{month}.csv",
    per_contract: bool = False,
) -> dict[str, Any]:
    """Capture every mutable input needed to query and render a report."""
    captured_at = datetime.now(UTC)
    selected_numbers = sorted(set(contract_numbers))
    contract_ids = _load_contract_ids(sync_session)
    contract_customers = _load_contract_customers(sync_session)
    missing = [
        number
        for number in selected_numbers
        if number not in contract_ids or number not in contract_customers
    ]
    if missing:
        raise BillingGenerationError(f"Unknown billing contracts: {', '.join(missing)}")

    contracts = [
        {
            "customer_name": contract_customers[number],
            "id": contract_ids[number],
            "number": number,
        }
        for number in selected_numbers
    ]
    selected_ids = {contract["id"] for contract in contracts}
    contract_id_to_number = {contract["id"]: contract["number"] for contract in contracts}

    prices = _load_prices(sync_session)
    price_facts = [
        {
            "metadata_field": price.metadata_field,
            "metadata_value": price.metadata_value,
            "resource_type": price.resource_type,
            "unit": price.unit,
            "unit_price": str(price.unit_price),
        }
        for price in prices
    ]
    all_overrides = _load_contract_overrides(sync_session)
    override_facts = [
        {
            "contract_number": contract_id_to_number[contract_id],
            "resource_type": resource_type,
            "unit_price": str(unit_price),
        }
        for contract_id in sorted(selected_ids)
        for resource_type, unit_price in sorted(all_overrides.get(contract_id, {}).items())
    ]
    all_rebates = _load_rebates(sync_session)
    rebate_facts = [
        {
            "contract_number": contract_id_to_number[contract_id],
            "rebate_percent": str(all_rebates[contract_id]),
        }
        for contract_id in sorted(selected_ids)
        if contract_id in all_rebates
    ]

    metric_fields: dict[str, set[str]] = {}
    for price in price_facts:
        metric_fields.setdefault(price["resource_type"], set())
        if price["metadata_field"]:
            metric_fields[price["resource_type"]].add(price["metadata_field"])
    for override in override_facts:
        metric_fields.setdefault(override["resource_type"], set())

    query_plan = []
    for metric in metric_fields:
        if metric in SYNTHETIC_RESOURCE_TYPES:
            continue
        product = GNOCCHI_PRODUCT_REGISTRY.get(metric)
        if product is None:
            raise BillingGenerationError(
                f"Unsupported metered billing resource type: {metric}"
            )
        query_plan.append(
            {
                "aggregation": product["aggregation"],
                "metadata_fields": sorted(
                    metric_fields[metric] | set(product["metadata_fields"])
                ),
                "metric": metric,
                "resource_type": product["resource_type"],
                "size_gb_scale": (
                    str(product["size_gb_scale"])
                    if product["size_gb_scale"] is not None
                    else None
                ),
                "source_metric": product["source_metric"],
                "unit": product["unit"],
            }
        )

    project_contracts = _get_project_contracts(conn)
    projects = sorted(
        (
            {"contract_number": contract, "id": project_id, "name": name}
            for project_id, (name, contract) in project_contracts.items()
            if contract in set(selected_numbers)
        ),
        key=lambda item: item["id"],
    )
    cinder_volume_types = (
        dict(sorted(_get_cinder_volume_type_names(conn).items()))
        if any(plan["metric"] == "volume.size" for plan in query_plan)
        else {}
    )
    synthetic_facts = _capture_synthetic_facts(
        sync_session,
        period_start,
        period_end,
        contract_id_to_number,
    )
    return {
        "artifact": {
            "filename_template": filename_template,
            "per_contract": per_contract,
        },
        "cinder_volume_types": cinder_volume_types,
        "contracts": contracts,
        "filename_variables": {
            "date": captured_at.strftime("%Y-%m-%d"),
            "day": f"{captured_at.day:02d}",
            "month": f"{period_start.month:02d}",
            "year": f"{period_start.year:04d}",
        },
        "period": {
            "end": _snapshot_datetime(period_end),
            "start": _snapshot_datetime(period_start),
        },
        "prices": price_facts,
        "projects": projects,
        "query_plan": query_plan,
        "rebates": rebate_facts,
        "synthetic": synthetic_facts,
        "overrides": override_facts,
        "version": BILLING_INPUT_SNAPSHOT_VERSION,
    }


def _price_after_override_and_rebate(
    *,
    base_price: Decimal,
    resource_type: str,
    contract_id: int | None,
    contract_overrides: dict[int, dict[str, Decimal]],
    rebates: dict[int, Decimal],
) -> Decimal:
    """Apply per-contract override + rebate to a unit price."""
    unit_price = base_price
    if contract_id and contract_id in contract_overrides:
        override = contract_overrides[contract_id].get(resource_type)
        if override is not None:
            unit_price = override
    if contract_id and contract_id in rebates:
        unit_price = unit_price * (1 - rebates[contract_id] / 100)
    return unit_price


def _cluster_management_fee(prices: list, worker_groups: int) -> tuple[Decimal, str]:
    """Return the package fee for a cluster's worker-group count.

    Published Kubernetes packages cover one through six worker groups. Larger
    clusters use the configured increment from the largest published package.
    VM and volume costs are intentionally not part of this fee: they remain
    metered products so flavor and storage changes affect the invoice.
    """
    package_prices = [
        (int(price.metadata_value), price)
        for price in prices
        if price.resource_type == "cluster_management_fee"
        and price.metadata_field == "worker_groups"
        and price.metadata_value is not None
        and price.metadata_value.isdecimal()
    ]
    if not package_prices:
        raise BillingGenerationError("No managed-cluster package prices configured")

    package_prices.sort(key=lambda item: item[0])
    for package_worker_groups, price in package_prices:
        if worker_groups == package_worker_groups:
            return price.unit_price, price.unit

    largest_worker_groups, largest_package = package_prices[-1]
    if worker_groups < package_prices[0][0]:
        raise BillingGenerationError(
            f"No managed-cluster package price for {worker_groups} worker groups"
        )
    increment = _find_price(prices, "cluster_management_fee_increment", {})
    if increment is None:
        raise BillingGenerationError("No managed-cluster package increment configured")
    extra_groups = worker_groups - largest_worker_groups
    return largest_package.unit_price + extra_groups * increment.unit_price, largest_package.unit


def _emit_synthetic_cluster_lines(
    sync_session: SyncSession,
    period_start: datetime,
    period_end: datetime,
    contract_set: set[str],
    prices: list,  # list[ResourcePrice]
    contract_overrides: dict[int, dict[str, Decimal]],
    rebates: dict[int, Decimal],
    contract_id_map: dict[str, int],
    contract_customer_map: dict[str, str],
    writer,
) -> None:
    """Emit cluster management/setup/addon fee lines for the given period.

    See plan §"Billing model" for the rules. Per-contract override and rebate
    apply via the same path as Gnocchi-metered lines.
    """
    contract_id_to_number = {v: k for k, v in contract_id_map.items()}

    # Load all provisioned clusters active during this period. We don't model
    # decommissioning yet, so "active" ≡ provisioned_at < period_end.
    clusters = list(
        sync_session.execute(
            select(TenantCluster).where(
                TenantCluster.provisioned_at.is_not(None),
                TenantCluster.provisioned_at < period_end,
            )
        ).scalars()
    )

    for cluster in clusters:
        cn = contract_id_to_number.get(cluster.contract_id)
        if not cn or cn not in contract_set:
            continue
        customer_name = contract_customer_map.get(cn)
        if customer_name is None:
            raise BillingGenerationError(f"No customer found for contract {cn}")
        contract_id = cluster.contract_id
        project_label = f"managed-cluster:{cluster.slug}"

        # 1. Package management fee: full month, never prorated.
        management_fee, management_unit = _cluster_management_fee(prices, cluster.worker_groups)
        unit_price = _price_after_override_and_rebate(
            base_price=management_fee,
            resource_type="cluster_management_fee",
            contract_id=contract_id,
            contract_overrides=contract_overrides,
            rebates=rebates,
        )
        writer.writerow(
            [
                customer_name,
                cn,
                project_label,
                "Cluster management fee",
                "1",
                management_unit,
                round(unit_price),
            ]
        )

        # 2. Initial setup fee: only in the period the cluster was provisioned.
        if period_start <= cluster.provisioned_at < period_end:
            ctrl = _find_price(prices, "cluster_setup_fee", {"group_type": "controllers"})
            if ctrl is not None:
                unit_price = _price_after_override_and_rebate(
                    base_price=ctrl.unit_price,
                    resource_type="cluster_setup_fee",
                    contract_id=contract_id,
                    contract_overrides=contract_overrides,
                    rebates=rebates,
                )
                writer.writerow(
                    [
                        customer_name,
                        cn,
                        project_label,
                        "Controller setup fee",
                        "1",
                        ctrl.unit,
                        round(unit_price),
                    ]
                )
            wkr = _find_price(prices, "cluster_setup_fee", {"group_type": "workers"})
            if wkr is not None and cluster.initial_worker_groups > 0:
                qty = Decimal(cluster.initial_worker_groups)
                unit_price = _price_after_override_and_rebate(
                    base_price=wkr.unit_price,
                    resource_type="cluster_setup_fee",
                    contract_id=contract_id,
                    contract_overrides=contract_overrides,
                    rebates=rebates,
                )
                writer.writerow(
                    [
                        customer_name,
                        cn,
                        project_label,
                        f"Worker setup fee (initial, {cluster.initial_worker_groups} groups)",
                        f"{qty:.0f}",
                        wkr.unit,
                        round(qty * unit_price),
                    ]
                )

    # 3. Resize expansion fees: any applied resize request in the period.
    resize_rows = list(
        sync_session.execute(
            select(ClusterRequest, TenantCluster)
            .join(TenantCluster, TenantCluster.id == ClusterRequest.cluster_id)
            .where(
                ClusterRequest.request_type == "resize",
                ClusterRequest.status == "applied",
                ClusterRequest.applied_at.is_not(None),
                ClusterRequest.applied_at >= period_start,
                ClusterRequest.applied_at < period_end,
            )
        ).all()
    )
    wkr = _find_price(prices, "cluster_setup_fee", {"group_type": "workers"})
    for cr, cluster in resize_rows:
        if wkr is None:
            break
        cn = contract_id_to_number.get(cluster.contract_id)
        if not cn or cn not in contract_set:
            continue
        customer_name = contract_customer_map.get(cn)
        if customer_name is None:
            raise BillingGenerationError(f"No customer found for contract {cn}")
        try:
            payload = json.loads(cr.payload)
        except (TypeError, ValueError):
            continue
        before = payload.get("before_worker_groups")
        target = payload.get("target_worker_groups")
        if before is None or target is None or target <= before:
            continue
        delta = Decimal(target - before)
        unit_price = _price_after_override_and_rebate(
            base_price=wkr.unit_price,
            resource_type="cluster_setup_fee",
            contract_id=cluster.contract_id,
            contract_overrides=contract_overrides,
            rebates=rebates,
        )
        writer.writerow(
            [
                customer_name,
                cn,
                f"managed-cluster:{cluster.slug}",
                f"Worker setup fee (expansion, +{int(delta)} groups)",
                f"{delta:.0f}",
                wkr.unit,
                round(delta * unit_price),
            ]
        )

    # 4. Addons: full month if active any time during the period.
    addon_rows = list(
        sync_session.execute(
            select(ClusterAddon, TenantCluster)
            .join(TenantCluster, TenantCluster.id == ClusterAddon.cluster_id)
            .where(
                ClusterAddon.enabled_at < period_end,
                (ClusterAddon.disabled_at.is_(None)) | (ClusterAddon.disabled_at > period_start),
            )
        ).all()
    )
    for addon, cluster in addon_rows:
        cn = contract_id_to_number.get(cluster.contract_id)
        if not cn or cn not in contract_set:
            continue
        customer_name = contract_customer_map.get(cn)
        if customer_name is None:
            raise BillingGenerationError(f"No customer found for contract {cn}")
        price = _find_price(prices, "cluster_addon_fee", {"addon": addon.addon_type})
        if price is None:
            continue
        unit_price = _price_after_override_and_rebate(
            base_price=price.unit_price,
            resource_type="cluster_addon_fee",
            contract_id=cluster.contract_id,
            contract_overrides=contract_overrides,
            rebates=rebates,
        )
        writer.writerow(
            [
                customer_name,
                cn,
                f"managed-cluster:{cluster.slug}",
                f"Addon: {addon.addon_type}",
                "1",
                price.unit,
                round(unit_price),
            ]
        )


def _query_gnocchi_usage(
    conn,
    begin: datetime,
    end: datetime,
    resource_type: str,
    metric_name: str,
    groupby_fields: list[str],
    project_ids: list[str],
    aggregate_across_resources: bool = False,
    normalization_period_seconds: Decimal | None = None,
) -> list[dict]:
    """Query history-aware usage and roll it up for pricing.

    Presence products retain per-resource groups so each non-empty hourly point
    represents one started resource-hour. Additive size products are summed by
    Gnocchi within each pricing metadata bucket before period normalization.
    """
    import httpx

    token = conn.auth_token
    gnocchi = "http://gnocchi-api.openstack.svc.cluster.local:8041"
    settings = get_settings()
    gnocchi_timeout = httpx.Timeout(
        settings.billing_gnocchi_timeout_seconds,
        connect=settings.billing_gnocchi_connect_timeout_seconds,
    )

    requested_project_id: str | None = None
    try:
        results_by_group: dict[tuple, dict] = {}
        begin_utc = begin.replace(tzinfo=UTC) if begin.tzinfo is None else begin.astimezone(UTC)
        end_utc = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
        query_period_seconds = Decimal(str((end_utc - begin_utc).total_seconds()))
        if query_period_seconds <= 0:
            raise BillingGenerationError("Billing period must have positive duration")
        period_seconds = normalization_period_seconds or query_period_seconds
        if period_seconds <= 0:
            raise BillingGenerationError("Billing normalization period must be positive")

        metadata_fields = [
            field
            for field in groupby_fields
            if field not in {"project_id", "id", "original_resource_id"}
        ]
        groupby = ["project_id", *metadata_fields]
        if not aggregate_across_resources:
            groupby[1:1] = ["id", "original_resource_id"]

        for requested_project_id in sorted(set(project_ids)):
            params = [
                ("start", begin_utc.isoformat()),
                ("stop", end_utc.isoformat()),
                ("granularity", str(BILLING_GRANULARITY_SECONDS)),
                ("fill", "dropna"),
                ("use_history", "true"),
                *(("groupby", field) for field in groupby),
            ]
            resp = httpx.post(
                f"{gnocchi}/v1/aggregates",
                params=params,
                json={
                    "resource_type": resource_type,
                    "search": {"=": {"project_id": requested_project_id}},
                    "operations": [
                        "aggregate",
                        "sum",
                        ["metric", metric_name, "mean"],
                    ],
                },
                headers={"X-Auth-Token": token},
                timeout=gnocchi_timeout,
            )
            if resp.status_code == 404:
                logger.debug(
                    "No Gnocchi %s/%s measurements in project %s between %s and %s",
                    resource_type,
                    metric_name,
                    requested_project_id,
                    begin_utc.isoformat(),
                    end_utc.isoformat(),
                )
                continue
            if resp.status_code != 200:
                message = (
                    f"Gnocchi aggregation for {resource_type}/{metric_name} "
                    f"returned HTTP {resp.status_code}"
                )
                logger.error(message)
                raise BillingGenerationError(message)

            response_content = getattr(resp, "content", b"")
            if len(response_content) > MAX_GNOCCHI_RESPONSE_BYTES:
                raise GnocchiShardTooLarge(
                    f"Gnocchi response for {resource_type}/{metric_name} exceeds "
                    f"{MAX_GNOCCHI_RESPONSE_BYTES} bytes"
                )
            groups = resp.json()
            if not isinstance(groups, list):
                raise BillingGenerationError(
                    f"Invalid Gnocchi response for {resource_type}/{metric_name}"
                )
            if len(groups) > MAX_GNOCCHI_GROUPS_PER_PROJECT:
                raise BillingGenerationError(
                    f"Gnocchi returned {len(groups)} groups for project "
                    f"{requested_project_id}; maximum is "
                    f"{MAX_GNOCCHI_GROUPS_PER_PROJECT}"
                )

            seen_source_groups: set[tuple] = set()
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("group"), dict):
                    raise BillingGenerationError(
                        f"Invalid Gnocchi group for {resource_type}/{metric_name}"
                    )
                group_info = group["group"]
                project_id = group_info.get("project_id")
                resource_id = group_info.get("id")
                original_resource_id = group_info.get("original_resource_id")
                if project_id != requested_project_id or (
                    not aggregate_across_resources
                    and (not resource_id or not original_resource_id)
                ):
                    raise BillingGenerationError(
                        f"Incomplete Gnocchi group for {resource_type}/{metric_name}"
                    )

                metadata = {}
                for field in metadata_fields:
                    value = group_info.get(field)
                    if value is None or value == "":
                        raise BillingGenerationError(
                            f"Gnocchi group for {resource_type}/{metric_name} is missing {field}"
                        )
                    metadata[field] = value

                metadata_key = tuple((field, metadata[field]) for field in sorted(metadata))
                if aggregate_across_resources:
                    source_group_key = (project_id, metadata_key)
                else:
                    source_group_key = (
                        project_id,
                        resource_id,
                        original_resource_id,
                        metadata_key,
                    )
                if source_group_key in seen_source_groups:
                    raise BillingGenerationError(
                        f"Duplicate Gnocchi group for {resource_type}/{metric_name}"
                    )
                seen_source_groups.add(source_group_key)

                try:
                    measures = group["measures"]["measures"]["aggregated"]
                except (KeyError, TypeError) as exc:
                    raise BillingGenerationError(
                        f"Invalid Gnocchi measures for {resource_type}/{metric_name}"
                    ) from exc
                if not isinstance(measures, list):
                    raise BillingGenerationError(
                        f"Invalid Gnocchi measures for {resource_type}/{metric_name}"
                    )
                if len(measures) > MAX_GNOCCHI_MEASURES_PER_GROUP:
                    raise GnocchiShardTooLarge(
                        f"Gnocchi returned too many measures for {resource_type}/{metric_name}"
                    )
                if not measures:
                    continue

                hours = Decimal(0)
                size_months = Decimal(0)
                seen_timestamps: set[datetime] = set()
                for measure in measures:
                    if not isinstance(measure, (list, tuple)) or len(measure) != 3:
                        raise BillingGenerationError(
                            f"Invalid Gnocchi measure for {resource_type}/{metric_name}"
                        )
                    timestamp_raw, granularity_raw, value_raw = measure
                    if not isinstance(timestamp_raw, str):
                        raise BillingGenerationError(
                            f"Invalid Gnocchi timestamp for {resource_type}/{metric_name}"
                        )
                    try:
                        timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise BillingGenerationError(
                            f"Invalid Gnocchi timestamp for {resource_type}/{metric_name}"
                        ) from exc
                    if timestamp.tzinfo is None:
                        raise BillingGenerationError(
                            f"Naive Gnocchi timestamp for {resource_type}/{metric_name}"
                        )
                    timestamp = timestamp.astimezone(UTC)
                    if timestamp < begin_utc or timestamp >= end_utc:
                        raise BillingGenerationError(
                            f"Out-of-range Gnocchi timestamp for "
                            f"{resource_type}/{metric_name}: {timestamp_raw}"
                        )
                    if (
                        timestamp.minute != 0
                        or timestamp.second != 0
                        or timestamp.microsecond != 0
                    ):
                        raise BillingGenerationError(
                            f"Unaligned Gnocchi timestamp for "
                            f"{resource_type}/{metric_name}: {timestamp_raw}"
                        )
                    # History grouping emits one sequence per resource, so
                    # additive groups legitimately repeat buckets to be summed.
                    if (
                        timestamp in seen_timestamps
                        and not aggregate_across_resources
                    ):
                        raise BillingGenerationError(
                            f"Duplicate Gnocchi timestamp for "
                            f"{resource_type}/{metric_name}: {timestamp_raw}"
                        )
                    seen_timestamps.add(timestamp)

                    if (
                        isinstance(granularity_raw, bool)
                        or isinstance(value_raw, bool)
                        or not isinstance(granularity_raw, (int, float))
                        or not isinstance(value_raw, (int, float))
                    ):
                        raise BillingGenerationError(
                            f"Invalid Gnocchi value for {resource_type}/{metric_name}"
                        )
                    try:
                        granularity = Decimal(str(granularity_raw))
                        value = Decimal(str(value_raw))
                    except (InvalidOperation, ValueError) as exc:
                        raise BillingGenerationError(
                            f"Invalid Gnocchi value for {resource_type}/{metric_name}"
                        ) from exc
                    if (
                        granularity != BILLING_GRANULARITY_SECONDS
                        or not value.is_finite()
                        or value < 0
                    ):
                        raise BillingGenerationError(
                            f"Invalid Gnocchi value for {resource_type}/{metric_name}"
                        )

                    hours += Decimal(1)
                    size_months += value * granularity / period_seconds

                result_key = (
                    project_id,
                    tuple((field, metadata[field]) for field in sorted(metadata)),
                )
                result = results_by_group.setdefault(
                    result_key,
                    {
                        "project_id": project_id,
                        "metric": metric_name,
                        "metadata": metadata,
                        "hours": Decimal(0),
                        "size_months": Decimal(0),
                    },
                )
                result["hours"] += hours
                result["size_months"] += size_months
        return list(results_by_group.values())
    except BillingGenerationError:
        raise
    except httpx.TimeoutException as exc:
        project_context = (
            f" in project {requested_project_id}" if requested_project_id is not None else ""
        )
        logger.exception(
            "Timed out querying Gnocchi for %s/%s%s",
            resource_type,
            metric_name,
            project_context,
        )
        raise GnocchiQueryTimeout(
            f"Timed out querying Gnocchi for {resource_type}/{metric_name}{project_context}"
        ) from exc
    except Exception as exc:
        logger.exception("Failed to query Gnocchi for %s/%s", resource_type, metric_name)
        raise BillingGenerationError(
            f"Failed to query Gnocchi for {resource_type}/{metric_name}"
        ) from exc


def _snapshot_price(
    prices: list[dict[str, Any]], metric: str, metadata: dict[str, str]
) -> dict[str, Any] | None:
    base_match = None
    for price in prices:
        if price["resource_type"] != metric:
            continue
        field = price["metadata_field"]
        value = price["metadata_value"]
        if field and value:
            if metadata.get(field) == value:
                return price
        elif not field:
            base_match = price
    return base_match


def _snapshot_unit_price(
    snapshot: dict[str, Any],
    contract_number: str,
    resource_type: str,
    base_price: Decimal,
) -> Decimal:
    unit_price = base_price
    for override in snapshot["overrides"]:
        if (
            override["contract_number"] == contract_number
            and override["resource_type"] == resource_type
        ):
            unit_price = Decimal(override["unit_price"])
            break
    for rebate in snapshot["rebates"]:
        if rebate["contract_number"] == contract_number:
            unit_price *= 1 - Decimal(rebate["rebate_percent"]) / 100
            break
    return unit_price


def _snapshot_management_fee(
    prices: list[dict[str, Any]], worker_groups: int
) -> tuple[Decimal, str]:
    package_prices = sorted(
        (
            (int(price["metadata_value"]), price)
            for price in prices
            if price["resource_type"] == "cluster_management_fee"
            and price["metadata_field"] == "worker_groups"
            and price["metadata_value"] is not None
            and price["metadata_value"].isdecimal()
        ),
        key=lambda item: item[0],
    )
    if not package_prices:
        raise BillingGenerationError("No managed-cluster package prices configured")
    for package_worker_groups, price in package_prices:
        if worker_groups == package_worker_groups:
            return Decimal(price["unit_price"]), price["unit"]
    largest_worker_groups, largest_package = package_prices[-1]
    if worker_groups < package_prices[0][0]:
        raise BillingGenerationError(
            f"No managed-cluster package price for {worker_groups} worker groups"
        )
    increment = _snapshot_price(prices, "cluster_management_fee_increment", {})
    if increment is None:
        raise BillingGenerationError("No managed-cluster package increment configured")
    return (
        Decimal(largest_package["unit_price"])
        + (worker_groups - largest_worker_groups) * Decimal(increment["unit_price"]),
        largest_package["unit"],
    )


def _render_snapshot_synthetic_lines(
    snapshot: dict[str, Any], contract_set: set[str], writer
) -> None:
    contracts = {item["number"]: item for item in snapshot["contracts"]}
    prices = snapshot["prices"]
    period_start = datetime.fromisoformat(snapshot["period"]["start"])
    period_end = datetime.fromisoformat(snapshot["period"]["end"])

    for cluster in snapshot["synthetic"]["clusters"]:
        contract_number = cluster["contract_number"]
        if contract_number not in contract_set:
            continue
        customer_name = contracts[contract_number]["customer_name"]
        project_label = f"managed-cluster:{cluster['slug']}"
        management_fee, management_unit = _snapshot_management_fee(
            prices, cluster["worker_groups"]
        )
        unit_price = _snapshot_unit_price(
            snapshot,
            contract_number,
            "cluster_management_fee",
            management_fee,
        )
        writer.writerow(
            [
                customer_name,
                contract_number,
                project_label,
                "Cluster management fee",
                "1",
                management_unit,
                round(unit_price),
            ]
        )

        provisioned_at = datetime.fromisoformat(cluster["provisioned_at"])
        if period_start <= provisioned_at < period_end:
            controller = _snapshot_price(
                prices, "cluster_setup_fee", {"group_type": "controllers"}
            )
            if controller is not None:
                unit_price = _snapshot_unit_price(
                    snapshot,
                    contract_number,
                    "cluster_setup_fee",
                    Decimal(controller["unit_price"]),
                )
                writer.writerow(
                    [
                        customer_name,
                        contract_number,
                        project_label,
                        "Controller setup fee",
                        "1",
                        controller["unit"],
                        round(unit_price),
                    ]
                )
            workers = _snapshot_price(
                prices, "cluster_setup_fee", {"group_type": "workers"}
            )
            initial_groups = cluster["initial_worker_groups"]
            if workers is not None and initial_groups > 0:
                quantity = Decimal(initial_groups)
                unit_price = _snapshot_unit_price(
                    snapshot,
                    contract_number,
                    "cluster_setup_fee",
                    Decimal(workers["unit_price"]),
                )
                writer.writerow(
                    [
                        customer_name,
                        contract_number,
                        project_label,
                        f"Worker setup fee (initial, {initial_groups} groups)",
                        f"{quantity:.0f}",
                        workers["unit"],
                        round(quantity * unit_price),
                    ]
                )

    workers = _snapshot_price(
        prices, "cluster_setup_fee", {"group_type": "workers"}
    )
    if workers is not None:
        for resize in snapshot["synthetic"]["resizes"]:
            contract_number = resize["contract_number"]
            if contract_number not in contract_set:
                continue
            quantity = Decimal(resize["delta_worker_groups"])
            unit_price = _snapshot_unit_price(
                snapshot,
                contract_number,
                "cluster_setup_fee",
                Decimal(workers["unit_price"]),
            )
            writer.writerow(
                [
                    contracts[contract_number]["customer_name"],
                    contract_number,
                    f"managed-cluster:{resize['slug']}",
                    f"Worker setup fee (expansion, +{int(quantity)} groups)",
                    f"{quantity:.0f}",
                    workers["unit"],
                    round(quantity * unit_price),
                ]
            )

    for addon in snapshot["synthetic"]["addons"]:
        contract_number = addon["contract_number"]
        if contract_number not in contract_set:
            continue
        price = _snapshot_price(
            prices, "cluster_addon_fee", {"addon": addon["addon_type"]}
        )
        if price is None:
            continue
        unit_price = _snapshot_unit_price(
            snapshot,
            contract_number,
            "cluster_addon_fee",
            Decimal(price["unit_price"]),
        )
        writer.writerow(
            [
                contracts[contract_number]["customer_name"],
                contract_number,
                f"managed-cluster:{addon['slug']}",
                f"Addon: {addon['addon_type']}",
                "1",
                price["unit"],
                round(unit_price),
            ]
        )


def render_billing_csv(
    snapshot: dict[str, Any],
    usage_by_metric: dict[str, list[dict]],
    contract_numbers: list[str] | None = None,
    delimiter: str = ";",
) -> str:
    """Render CSV using only immutable inputs and precomputed usage."""
    if snapshot.get("version") != BILLING_INPUT_SNAPSHOT_VERSION:
        raise BillingGenerationError("Unsupported billing input snapshot version")
    available_contracts = {item["number"]: item for item in snapshot["contracts"]}
    contract_set = (
        set(available_contracts)
        if contract_numbers is None
        else set(contract_numbers)
    )
    if not contract_set.issubset(available_contracts):
        raise BillingGenerationError("Rendered contracts are not present in snapshot")
    projects = {item["id"]: item for item in snapshot["projects"]}

    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    for product in snapshot["query_plan"]:
        metric = product["metric"]
        usage = usage_by_metric.get(metric, [])
        if metric == "volume.size" and usage:
            usage = _canonicalize_volume_usage(
                usage, snapshot["cinder_volume_types"]
            )
        for entry in usage:
            project = projects.get(entry["project_id"])
            if project is None or project["contract_number"] not in contract_set:
                continue
            contract_number = project["contract_number"]
            metadata = dict(entry.get("metadata", {}))
            price = _snapshot_price(snapshot["prices"], metric, metadata)
            if price is None:
                raise BillingGenerationError(
                    f"No price for project {project['name']}, product {metric}, "
                    f"metadata {metadata}"
                )
            if product["size_gb_scale"] is not None:
                quantity = Decimal(str(entry["size_months"])) * Decimal(
                    product["size_gb_scale"]
                )
            else:
                quantity = Decimal(str(entry["hours"]))
            unit_price = _snapshot_unit_price(
                snapshot,
                contract_number,
                metric,
                Decimal(price["unit_price"]),
            )
            label = metric
            if metadata:
                label = f"{metric} ({', '.join(str(value) for value in metadata.values())})"
            writer.writerow(
                [
                    available_contracts[contract_number]["customer_name"],
                    contract_number,
                    project["name"],
                    label,
                    f"{quantity:.2f}",
                    price["unit"],
                    round(quantity * unit_price),
                ]
            )

    _render_snapshot_synthetic_lines(snapshot, contract_set, writer)
    data_rows = output.getvalue()
    if not data_rows:
        return ""
    report = io.StringIO()
    report_writer = csv.writer(report, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    report_writer.writerow(BILLING_CSV_HEADER)
    report.write(data_rows)
    return UTF8_BOM + report.getvalue()


def query_billing_snapshot_usage(
    conn,
    snapshot: dict[str, Any],
) -> dict[str, list[dict]]:
    """Execute the frozen query plan for the frozen project scope."""
    begin = datetime.fromisoformat(snapshot["period"]["start"])
    end = datetime.fromisoformat(snapshot["period"]["end"])
    project_ids = [project["id"] for project in snapshot["projects"]]
    return {
        product["metric"]: _query_gnocchi_usage(
            conn,
            begin,
            end,
            product["resource_type"],
            product["source_metric"],
            product["metadata_fields"],
            project_ids,
            aggregate_across_resources=(
                product["aggregation"] == "additive_size"
            ),
        )
        for product in snapshot["query_plan"]
    }


def generate_billing_csv(
    db_url: str,
    cloud_name: str,
    contract_numbers: list[str],
    period_start: datetime,
    period_end: datetime,
    delimiter: str = ";",
    precomputed_usage: dict[str, list[dict]] | None = None,
) -> str:
    """Capture live inputs, query usage, and render a billing CSV synchronously."""
    sync_url = db_url.replace("+asyncpg", "")
    if sync_url.startswith("postgresql://"):
        sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(sync_url)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    try:
        conn = openstack.connect(cloud=cloud_name)
        snapshot = capture_billing_input_snapshot(
            db,
            conn,
            contract_numbers,
            period_start,
            period_end,
        )
        usage = (
            query_billing_snapshot_usage(conn, snapshot)
            if precomputed_usage is None
            else precomputed_usage
        )
        return render_billing_csv(snapshot, usage, contract_numbers, delimiter)
    finally:
        db.close()
        engine.dispose()


# --- Filename template ---


def resolve_template(template: str, **kwargs: str) -> str:
    """Resolve a filename template with the given variables."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    # Sanitize for filesystem safety
    result = re.sub(r"[^\w\-.]", "_", result)
    return result


# --- Delivery methods ---


def encode_billing_csv(content: str) -> bytes:
    """Encode generated billing CSV text without altering its UTF-8 BOM."""
    return content.encode("utf-8")


async def deliver_webdav(
    url: str, username: str, password: str, filename: str, content: str
) -> None:
    """Upload a file to a WebDAV endpoint.

    Durable retries PUT the same filename, so retrying replaces the same remote
    resource rather than creating another delivery.

    Re-runs the SSRF allowlist check at delivery time as defence-in-depth
    against jobs whose stored URL pre-dates a tightened allowlist or whose
    DNS resolution has shifted to internal addresses.
    """
    from app.url_safety import validate_webdav_url

    settings = get_settings()
    validate_webdav_url(url, settings.webdav_allowed_hosts)

    full_url = url.rstrip("/") + "/" + filename
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        resp = await client.put(
            full_url,
            content=encode_billing_csv(content),
            auth=(username, password),
            headers={"Content-Type": "text/csv; charset=utf-8"},
        )
        # WebDAV success is 200/201/204. Treat anything else (including an
        # unfollowed 3xx) as a failure. raise_for_status() discards the
        # response body, but Nextcloud/Sabre puts the actual cause there
        # (e.g. the Sabre exception class and message), so capture it.
        if not resp.is_success:
            body = resp.text.strip()[:500]
            logger.error(
                "WebDAV PUT to %s failed: HTTP %d; body: %s",
                full_url,
                resp.status_code,
                body or "(empty body)",
            )
            raise RuntimeError(
                f"WebDAV PUT returned HTTP {resp.status_code}: {body or '(empty body)'}"
            )
    logger.info("Delivered %s to WebDAV (HTTP %d): %s", filename, resp.status_code, url)


async def deliver_email(recipient: str, subject: str, filename: str, content: str) -> None:
    """Send a billing CSV as an at-least-once email attachment.

    SMTP acceptance and the caller's durable sent checkpoint cannot be one
    transaction. A crash between them can therefore cause a duplicate send.
    """
    settings = get_settings()
    if not settings.smtp_host:
        raise RuntimeError("SMTP not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = recipient
    # Date + Message-ID let downstream MTAs dedupe on retry; without them a
    # single send can land as two delivered copies.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.rsplit("@", 1)[-1])
    msg.set_content(f"Billing report: {filename}")
    msg.add_attachment(
        encode_billing_csv(content),
        maintype="text",
        subtype="csv",
        filename=filename,
        params={"charset": "utf-8"},
    )

    def _send():
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_username:
                smtp.starttls()
                smtp.ehlo()
                # Pin AUTH LOGIN: some relays (smtp.sunet.se) drop the
                # connection on smtplib's default PLAIN-first attempt,
                # masking a clean 535 as SMTPServerDisconnected.
                smtp.user = settings.smtp_username
                smtp.password = settings.smtp_password
                smtp.auth("LOGIN", smtp.auth_login)
            smtp.send_message(msg)

    await asyncio.to_thread(_send)
    logger.info("Emailed %s to %s", filename, recipient)


# --- Job execution ---


def _decrypt_config(delivery_config_json: str) -> dict:
    """Parse delivery config JSON and fail closed for invalid ciphertext."""
    config = json.loads(delivery_config_json)
    if "password" in config and config["password"]:
        password = config["password"]
        encrypted = password.startswith("fernet:v1:") or password.startswith("gAAAA")
        if not encrypted:
            logger.warning("Using legacy plaintext billing delivery password")
            return config
        ciphertext = password.removeprefix("fernet:v1:")
        try:
            config["password"] = decrypt_value(ciphertext)
        except Exception as exc:
            raise BillingGenerationError(
                "Unable to decrypt billing delivery credentials"
            ) from exc
    return config


async def iter_billing_files(
    settings,
    contract_numbers: list[str],
    filename_template: str,
    per_contract: bool,
    period_start: datetime,
    period_end: datetime,
) -> AsyncIterator[tuple[str, str]]:
    """Generate named billing CSV files one at a time."""
    now = datetime.now(UTC)
    template_vars = {
        "year": f"{period_start.year:04d}",
        "month": f"{period_start.month:02d}",
        "day": f"{now.day:02d}",
        "date": now.strftime("%Y-%m-%d"),
    }

    filenames = set()

    if per_contract:
        if "{contract}" not in filename_template:
            stem, separator, suffix = filename_template.rpartition(".")
            if separator:
                filename_template = f"{stem}-{{contract}}.{suffix}"
            else:
                filename_template = f"{filename_template}-{{contract}}"
        for cn in contract_numbers:
            csv_content = await asyncio.to_thread(
                generate_billing_csv,
                settings.database_url,
                settings.openstack_cloud,
                [cn],
                period_start,
                period_end,
            )
            if not csv_content.strip():
                continue

            cn_vars = {**template_vars, "contract": cn}
            filename = resolve_template(filename_template, **cn_vars)
            if filename in filenames:
                raise BillingGenerationError(
                    "Per-contract filename template produced duplicate filenames"
                )
            filenames.add(filename)
            yield filename, csv_content
    else:
        csv_content = await asyncio.to_thread(
            generate_billing_csv,
            settings.database_url,
            settings.openstack_cloud,
            contract_numbers,
            period_start,
            period_end,
        )
        if not csv_content.strip():
            return
        filename = resolve_template(filename_template, **template_vars)
        yield filename, csv_content


async def generate_billing_files(
    settings,
    contract_numbers: list[str],
    filename_template: str,
    per_contract: bool,
    period_start: datetime,
    period_end: datetime,
) -> list[tuple[str, str]]:
    """Collect generated files for callers that need all files in memory."""
    return [
        item
        async for item in iter_billing_files(
            settings,
            contract_numbers,
            filename_template,
            per_contract,
            period_start,
            period_end,
        )
    ]


async def generate_and_deliver(
    settings,
    contract_numbers: list[str],
    delivery_method: str,
    config: dict,
    filename_template: str,
    per_contract: bool,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """Generate billing CSV files and deliver them through a configured method."""
    files_delivered = 0
    async for filename, csv_content in iter_billing_files(
        settings,
        contract_numbers,
        filename_template,
        per_contract,
        period_start,
        period_end,
    ):
        await _deliver(delivery_method, config, filename, csv_content)
        files_delivered += 1

    if not per_contract and files_delivered == 0:
        raise BillingGenerationError(
            "Billing report is empty; refusing to deliver an empty combined file"
        )
    return files_delivered


async def execute_job(
    session: AsyncSession,
    job: BillingJob,
    year: int | None = None,
    month: int | None = None,
) -> BillingJobRun:
    """Create an active run and enqueue its frozen durable report."""
    settings = get_settings()
    period_start, period_end = get_billing_period(year, month)
    job_id = job.id

    failed_run = (
        await session.execute(
            select(BillingJobRun)
            .where(
                BillingJobRun.billing_job_id == job_id,
                BillingJobRun.billing_period_start == period_start,
                BillingJobRun.billing_period_end == period_end,
                BillingJobRun.status == "error",
            )
            .order_by(BillingJobRun.started_at.desc(), BillingJobRun.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if failed_run is not None:
        failed_report = (
            await session.execute(
                select(BillingReport).where(
                    BillingReport.billing_job_run_id == failed_run.id,
                    BillingReport.status == "failed",
                )
            )
        ).scalars().first()
        if failed_report is not None:
            await requeue_failed_report(session, failed_report)
            await session.commit()
            await session.refresh(failed_run)
            return failed_run

    run = BillingJobRun(
        billing_job_id=job_id,
        billing_period_start=period_start,
        billing_period_end=period_end,
        status="running",
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(
            select(BillingJobRun).where(
                BillingJobRun.billing_job_id == job_id,
                BillingJobRun.billing_period_start == period_start,
                BillingJobRun.billing_period_end == period_end,
                BillingJobRun.status == "running",
            )
        )
        active_run = existing.scalars().first()
        if active_run is None:
            raise
        logger.info(
            "Billing job %d already has active run %d for %s to %s",
            job_id,
            active_run.id,
            period_start,
            period_end,
        )
        return active_run

    if job.all_contracts:
        if job.owner_sub in settings.admin_users:
            contract_numbers = list(
                (await session.execute(select(Contract.contract_number))).scalars()
            )
        else:
            contract_numbers = list(
                (
                    await session.execute(
                        select(Contract.contract_number)
                        .join(ContractAccess)
                        .where(ContractAccess.user_sub == job.owner_sub)
                    )
                ).scalars()
            )
    else:
        selected_contracts = select(Contract.contract_number).join(
            BillingJobContract,
            BillingJobContract.contract_id == Contract.id,
        )
        if job.owner_sub not in settings.admin_users:
            selected_contracts = selected_contracts.join(ContractAccess).where(
                ContractAccess.user_sub == job.owner_sub
            )
        contract_numbers = list(
            (
                await session.execute(
                    selected_contracts.where(
                        BillingJobContract.billing_job_id == job_id
                    )
                )
            ).scalars()
        )

    completed_at = _utc_now() if not contract_numbers else None
    if completed_at is not None:
        run.status = "success"
        run.completed_at = completed_at
        run.files_delivered = 0
    report = BillingReport(
        id=str(uuid4()),
        billing_job_run_id=run.id,
        requested_by_sub=job.owner_sub,
        status="succeeded" if completed_at is not None else "queued",
        billing_period_start=period_start,
        billing_period_end=period_end,
        contract_numbers_json=json.dumps(sorted(set(contract_numbers))),
        filename_template=job.filename_template,
        per_contract=job.per_contract,
        delivery_method=job.delivery_method,
        delivery_config=job.delivery_config if completed_at is None else None,
        progress_current=0,
        progress_total=0,
        completed_at=completed_at,
    )
    session.add(report)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(
            select(BillingJobRun).where(
                BillingJobRun.billing_job_id == job_id,
                BillingJobRun.billing_period_start == period_start,
                BillingJobRun.billing_period_end == period_end,
                BillingJobRun.status == "running",
            )
        )
        active_run = existing.scalars().first()
        if active_run is None:
            raise
        return active_run
    await session.refresh(run)
    return run


async def _deliver(method: str, config: dict, filename: str, content: str) -> None:
    """Dispatch to the appropriate delivery method."""
    if method == "webdav":
        await deliver_webdav(
            config["url"],
            config.get("username", ""),
            config.get("password", ""),
            filename,
            content,
        )
    elif method == "email":
        subject = f"Billing report: {filename}"
        await deliver_email(config["recipient"], subject, filename, content)
    else:
        raise ValueError(f"Unknown delivery method: {method}")


# --- Schedule matching ---


def should_run_now(schedule: str, now: datetime, window_minutes: int = 15) -> bool:
    """Check if a cron schedule has a trigger within the last window."""
    cron = croniter(schedule, now)
    last_scheduled = cron.get_prev(datetime)
    window_start = now - timedelta(minutes=window_minutes)
    return window_start <= last_scheduled <= now


async def run_due_jobs(session: AsyncSession) -> list[BillingJobRun]:
    """Find due billing jobs and enqueue their durable reports."""
    now = _utc_now()
    result = await session.execute(
        select(BillingJob).where(BillingJob.enabled == True)  # noqa: E712
    )
    jobs = result.scalars().all()
    runs = []

    for job in jobs:
        if not should_run_now(job.schedule, now):
            continue

        period_start, period_end = get_billing_period()

        # Check for existing run this period
        existing = await session.execute(
            select(BillingJobRun).where(
                BillingJobRun.billing_job_id == job.id,
                BillingJobRun.billing_period_start == period_start,
                BillingJobRun.billing_period_end == period_end,
                BillingJobRun.status.in_(["running", "success"]),
            )
        )
        if existing.scalars().first() is not None:
            continue

        logger.info("Enqueueing due billing job %d: %s", job.id, job.name)
        run = await execute_job(session, job)
        runs.append(run)

    return runs
