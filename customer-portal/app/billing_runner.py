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

import httpx
import openstack
from croniter import croniter
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.crypto import decrypt_value
from app.models import (
    BillingJob,
    BillingJobContract,
    BillingJobRun,
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
MAX_BILLING_PROJECTS = 1000
MAX_GNOCCHI_GROUPS_PER_PROJECT = 10000
MAX_GNOCCHI_MEASURES_PER_GROUP = 20000
MAX_GNOCCHI_RESPONSE_BYTES = 10 * 1024 * 1024
GNOCCHI_RESOURCE_PAGE_SIZE = 100
MAX_GNOCCHI_RESOURCE_PAGES = 100
MAX_GNOCCHI_RESOURCES_PER_SEARCH = 10000
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

# The `instance` product is billed from CPU sample presence because Ceilometer
# does not emit a continuously sampled metric named `instance`.
GNOCCHI_PRODUCT_REGISTRY = {
    "instance": {
        "resource_type": "instance",
        "source_metric": "cpu",
        "metadata_fields": {"flavor_name"},
        "unit": "hour",
        "size_gb_scale": None,
    },
    "volume.size": {
        "resource_type": "volume",
        "source_metric": "volume.size",
        "metadata_fields": {"volume_type"},
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "volume.snapshot.size": {
        "resource_type": "volume",
        "source_metric": "volume.snapshot.size",
        "metadata_fields": set(),
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "volume.backup.size": {
        "resource_type": "volume",
        "source_metric": "volume.backup.size",
        "metadata_fields": set(),
        "unit": "GB-month",
        "size_gb_scale": Decimal(1),
    },
    "radosgw.objects.size": {
        "resource_type": "ceph_account",
        "source_metric": "radosgw.objects.size",
        "metadata_fields": set(),
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

CINDER_METRIC_FAMILY_MARKERS = {
    "volume.size": {"volume", "volume.size"},
    "volume.snapshot.size": {"snapshot.size", "volume.snapshot.size"},
    "volume.backup.size": {"backup.size", "volume.backup.size"},
}


class BillingGenerationError(RuntimeError):
    """Raised when a billing report cannot be generated completely."""


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
    raise BillingGenerationError(
        f"Cinder volume type {value} is unknown or no longer active"
    )


def _canonicalize_volume_usage(usage: list[dict], type_names: dict[str, str]) -> list[dict]:
    """Resolve Cinder type IDs and roll equivalent historical groups together."""
    canonical: dict[tuple, dict] = {}
    for entry in usage:
        metadata = dict(entry.get("metadata", {}))
        raw_volume_type = metadata.get("volume_type")
        if not isinstance(raw_volume_type, str) or not raw_volume_type:
            raise BillingGenerationError("Volume usage is missing volume_type")
        metadata["volume_type"] = _resolve_cinder_volume_type(
            raw_volume_type, type_names
        )

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

                entry["metadata_fields"].append({
                    "field": field,
                    "values": sorted(values),
                })

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
            contract_number = contract_tags[0][len(CONTRACT_TAG_PREFIX):]
            if not contract_number:
                raise BillingGenerationError(
                    f"Project {project.name} has an empty contract tag"
                )
            project_map[project.id] = (project.name, contract_number)
    return project_map


def _load_prices(sync_session: SyncSession) -> list[ResourcePrice]:
    """Load all resource prices, ordered so specific (metadata) prices come first."""
    result = sync_session.execute(
        select(ResourcePrice).order_by(
            ResourcePrice.resource_type,
            ResourcePrice.metadata_field.desc(),  # non-null first
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

        # 1. Per-VM management fee: full month, never prorated.
        mgmt = _find_price(prices, "cluster_management_fee", {})
        if mgmt is not None:
            qty = Decimal(3 + 3 * cluster.worker_groups)
            unit_price = _price_after_override_and_rebate(
                base_price=mgmt.unit_price,
                resource_type="cluster_management_fee",
                contract_id=contract_id,
                contract_overrides=contract_overrides,
                rebates=rebates,
            )
            cost = qty * unit_price
            writer.writerow(
                [
                    customer_name,
                    cn,
                    project_label,
                    "Cluster management fee",
                    f"{qty:.0f}",
                    mgmt.unit,
                    round(cost),
                ]
            )

        # 2. Initial setup fee: only in the period the cluster was provisioned.
        if period_start <= cluster.provisioned_at < period_end:
            ctrl = _find_price(
                prices, "cluster_setup_fee", {"group_type": "controllers"}
            )
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
            wkr = _find_price(
                prices, "cluster_setup_fee", {"group_type": "workers"}
            )
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
                (ClusterAddon.disabled_at.is_(None))
                | (ClusterAddon.disabled_at > period_start),
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
        price = _find_price(
            prices, "cluster_addon_fee", {"addon": addon.addon_type}
        )
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


def _project_had_gnocchi_resources(
    token: str,
    gnocchi: str,
    resource_type: str,
    metric_name: str,
    project_id: str,
    begin: datetime,
    end: datetime,
) -> bool:
    """Return whether a project had resources expected to emit this metric.

    Search current resources because old revisions can retain a null ended_at
    after the resource's current revision has been closed.
    """
    search = {
        "and": [
            {"=": {"project_id": project_id}},
            {"<": {"started_at": end.isoformat()}},
            {
                "or": [
                    {">": {"ended_at": begin.isoformat()}},
                    {"=": {"ended_at": None}},
                ]
            },
        ]
    }
    family_markers = (
        CINDER_METRIC_FAMILY_MARKERS.get(metric_name) if resource_type == "volume" else None
    )
    marker = None
    previous_id = None
    resource_count = 0

    for page_number in range(1, MAX_GNOCCHI_RESOURCE_PAGES + 1):
        params = [
            ("limit", str(GNOCCHI_RESOURCE_PAGE_SIZE)),
            ("sort", "id:asc"),
        ]
        if marker is not None:
            params.append(("marker", marker))

        response = httpx.post(
            f"{gnocchi}/v1/search/resource/{resource_type}",
            params=params,
            json=search,
            headers={"X-Auth-Token": token},
            timeout=60,
        )
        response_content = getattr(response, "content", b"")
        if len(response_content) > MAX_GNOCCHI_RESPONSE_BYTES:
            raise BillingGenerationError(
                f"Gnocchi resource search response for {resource_type}/{metric_name} "
                f"exceeds {MAX_GNOCCHI_RESPONSE_BYTES} bytes"
            )
        if response.status_code != 200:
            raise BillingGenerationError(
                f"Unable to classify empty Gnocchi result for "
                f"{resource_type}/{metric_name}: HTTP {response.status_code}"
            )
        try:
            resources = response.json()
        except (TypeError, ValueError) as exc:
            raise BillingGenerationError(
                f"Invalid Gnocchi resource search response for {resource_type}/{metric_name}"
            ) from exc
        if not isinstance(resources, list) or len(resources) > GNOCCHI_RESOURCE_PAGE_SIZE:
            raise BillingGenerationError(
                f"Invalid Gnocchi resource search response for {resource_type}/{metric_name}"
            )
        if not resources:
            return False

        expected_family_found = False
        for resource in resources:
            if not isinstance(resource, dict):
                raise BillingGenerationError(
                    f"Invalid Gnocchi resource for {resource_type}/{metric_name}"
                )
            resource_id = resource.get("id")
            metrics = resource.get("metrics")
            if (
                not isinstance(resource_id, str)
                or not resource_id.strip()
                or not isinstance(metrics, dict)
                or any(
                    not isinstance(name, str)
                    or not name.strip()
                    or not isinstance(metric_id, str)
                    or not metric_id.strip()
                    for name, metric_id in metrics.items()
                )
            ):
                raise BillingGenerationError(
                    f"Invalid Gnocchi resource for {resource_type}/{metric_name}"
                )
            if previous_id is not None and resource_id <= previous_id:
                raise BillingGenerationError(
                    f"Non-advancing Gnocchi resource marker for {resource_type}/{metric_name}"
                )
            previous_id = resource_id
            resource_count += 1
            if resource_count > MAX_GNOCCHI_RESOURCES_PER_SEARCH:
                raise BillingGenerationError(
                    f"Gnocchi resource search for {resource_type}/{metric_name} "
                    f"exceeds {MAX_GNOCCHI_RESOURCES_PER_SEARCH} resources"
                )
            if family_markers is None or family_markers.intersection(metrics):
                expected_family_found = True

        if expected_family_found:
            return True
        if len(resources) < GNOCCHI_RESOURCE_PAGE_SIZE:
            return False
        if (
            page_number == MAX_GNOCCHI_RESOURCE_PAGES
            or resource_count == MAX_GNOCCHI_RESOURCES_PER_SEARCH
        ):
            raise BillingGenerationError(
                f"Gnocchi resource search for {resource_type}/{metric_name} "
                "exceeded pagination limits"
            )
        next_marker = resources[-1]["id"]
        if next_marker == marker:
            raise BillingGenerationError(
                f"Non-advancing Gnocchi resource marker for {resource_type}/{metric_name}"
            )
        marker = next_marker

    raise BillingGenerationError(
        f"Gnocchi resource search for {resource_type}/{metric_name} exceeded pagination limits"
    )


def _query_gnocchi_usage(
    conn, begin: datetime, end: datetime, resource_type: str, metric_name: str,
    groupby_fields: list[str], project_ids: list[str],
) -> list[dict]:
    """Query history-aware per-resource usage and roll it up for pricing.

    Each non-empty hourly series point represents one started resource-hour. Size
    metrics additionally use the point value to calculate period-normalized size.
    """
    import httpx

    token = conn.auth_token
    gnocchi = "http://gnocchi-api.openstack.svc.cluster.local:8041"

    try:
        if len(project_ids) > MAX_BILLING_PROJECTS:
            raise BillingGenerationError(
                f"Billing scope has {len(project_ids)} projects; "
                f"maximum is {MAX_BILLING_PROJECTS}"
            )

        results_by_group: dict[tuple, dict] = {}
        begin_utc = begin.replace(tzinfo=UTC) if begin.tzinfo is None else begin.astimezone(UTC)
        end_utc = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
        period_seconds = Decimal(str((end_utc - begin_utc).total_seconds()))
        if period_seconds <= 0:
            raise BillingGenerationError("Billing period must have positive duration")

        metadata_fields = [
            field
            for field in groupby_fields
            if field not in {"project_id", "id", "original_resource_id"}
        ]
        groupby = ["project_id", "id", "original_resource_id", *metadata_fields]

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
                timeout=60,
            )
            if resp.status_code == 404:
                if not _project_had_gnocchi_resources(
                    token,
                    gnocchi,
                    resource_type,
                    metric_name,
                    requested_project_id,
                    begin_utc,
                    end_utc,
                ):
                    logger.debug(
                        "No Gnocchi %s resources in project %s",
                        resource_type,
                        requested_project_id,
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
                raise BillingGenerationError(
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

            seen_resource_groups: set[tuple] = set()
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("group"), dict):
                    raise BillingGenerationError(
                        f"Invalid Gnocchi group for {resource_type}/{metric_name}"
                    )
                group_info = group["group"]
                project_id = group_info.get("project_id")
                resource_id = group_info.get("id")
                original_resource_id = group_info.get("original_resource_id")
                if (
                    project_id != requested_project_id
                    or not resource_id
                    or not original_resource_id
                ):
                    raise BillingGenerationError(
                        f"Incomplete Gnocchi group for {resource_type}/{metric_name}"
                    )

                metadata = {}
                for field in metadata_fields:
                    value = group_info.get(field)
                    if value is None or value == "":
                        raise BillingGenerationError(
                            f"Gnocchi group for {resource_type}/{metric_name} "
                            f"is missing {field}"
                        )
                    metadata[field] = value

                resource_group_key = (
                    project_id,
                    resource_id,
                    original_resource_id,
                    tuple((field, metadata[field]) for field in sorted(metadata)),
                )
                if resource_group_key in seen_resource_groups:
                    raise BillingGenerationError(
                        f"Duplicate Gnocchi group for {resource_type}/{metric_name}"
                    )
                seen_resource_groups.add(resource_group_key)

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
                    raise BillingGenerationError(
                        f"Gnocchi returned too many measures for "
                        f"{resource_type}/{metric_name}"
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
                        timestamp = datetime.fromisoformat(
                            timestamp_raw.replace("Z", "+00:00")
                        )
                    except ValueError as exc:
                        raise BillingGenerationError(
                            f"Invalid Gnocchi timestamp for {resource_type}/{metric_name}"
                        ) from exc
                    if timestamp.tzinfo is None:
                        raise BillingGenerationError(
                            f"Naive Gnocchi timestamp for {resource_type}/{metric_name}"
                        )
                    timestamp = timestamp.astimezone(UTC)
                    if (
                        timestamp < begin_utc
                        or timestamp >= end_utc
                        or timestamp.minute != 0
                        or timestamp.second != 0
                        or timestamp.microsecond != 0
                        or timestamp in seen_timestamps
                    ):
                        raise BillingGenerationError(
                            f"Invalid Gnocchi timestamp for {resource_type}/{metric_name}"
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
    except Exception as exc:
        logger.exception("Failed to query Gnocchi for %s/%s", resource_type, metric_name)
        raise BillingGenerationError(
            f"Failed to query Gnocchi for {resource_type}/{metric_name}"
        ) from exc


def generate_billing_csv(
    db_url: str,
    cloud_name: str,
    contract_numbers: list[str],
    period_start: datetime,
    period_end: datetime,
    delimiter: str = ";",
) -> str:
    """Generate billing CSV for the given contracts and period. Runs synchronously."""
    sync_url = db_url.replace("+asyncpg", "")
    if sync_url.startswith("postgresql://"):
        sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(sync_url)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    try:
        # Load prices from DB
        prices = _load_prices(db)
        contract_overrides = _load_contract_overrides(db)
        rebates = _load_rebates(db)
        contract_id_map = _load_contract_ids(db)
        contract_customer_map = _load_contract_customers(db)

        # Determine which metric types to query, and their metadata fields
        # Group prices by resource_type to find which metadata fields are used
        metric_metadata_fields: dict[str, set[str]] = {}
        for p in prices:
            if p.resource_type not in metric_metadata_fields:
                metric_metadata_fields[p.resource_type] = set()
            if p.metadata_field:
                metric_metadata_fields[p.resource_type].add(p.metadata_field)

        # Also include metrics from contract overrides
        for overrides in contract_overrides.values():
            for rt in overrides:
                if rt not in metric_metadata_fields:
                    metric_metadata_fields[rt] = set()

        conn = openstack.connect(cloud=cloud_name)
        project_contracts = _get_project_contracts(conn)
        contract_set = set(contract_numbers)
        project_ids = [
            project_id
            for project_id, (_, contract_number) in project_contracts.items()
            if contract_number in contract_set
        ]

        # These are computed from the DB by _emit_synthetic_cluster_lines,
        # not metered by Gnocchi; querying them only yields 404s.
        SYNTHETIC_RESOURCE_TYPES = {
            "cluster_management_fee",
            "cluster_setup_fee",
            "cluster_addon_fee",
        }

        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        volume_type_names = None

        for metric, meta_fields in metric_metadata_fields.items():
            if metric in SYNTHETIC_RESOURCE_TYPES:
                continue
            product = GNOCCHI_PRODUCT_REGISTRY.get(metric)
            if product is None:
                raise BillingGenerationError(
                    f"Unsupported metered billing resource type: {metric}"
                )
            resource_type = product["resource_type"]
            source_metric = product["source_metric"]
            groupby = sorted(set(meta_fields) | set(product["metadata_fields"]))

            usage = _query_gnocchi_usage(
                conn,
                period_start,
                period_end,
                resource_type,
                source_metric,
                groupby,
                project_ids,
            )
            if metric == "volume.size" and usage and volume_type_names is None:
                volume_type_names = _get_cinder_volume_type_names(conn)
            if metric == "volume.size" and usage:
                usage = _canonicalize_volume_usage(usage, volume_type_names)

            for entry in usage:
                pid = entry["project_id"]
                if pid not in project_contracts:
                    continue
                project_name, cn = project_contracts[pid]
                if cn not in contract_set:
                    continue
                customer_name = contract_customer_map.get(cn)
                if customer_name is None:
                    raise BillingGenerationError(f"No customer found for contract {cn}")

                metadata = dict(entry.get("metadata", {}))

                # Find the best matching price (specific metadata > base)
                price = _find_price(prices, metric, metadata)
                if not price:
                    raise BillingGenerationError(
                        f"No price for project {project_name}, product {metric}, "
                        f"metadata {metadata}"
                    )

                unit = price.unit
                if product["size_gb_scale"] is not None:
                    quantity = Decimal(str(entry["size_months"])) * product["size_gb_scale"]
                else:
                    quantity = Decimal(str(entry["hours"]))

                # Determine unit_price: contract override > global
                contract_id = contract_id_map.get(cn)
                unit_price = price.unit_price
                if contract_id and contract_id in contract_overrides:
                    override_price = contract_overrides[contract_id].get(metric, None)
                    if override_price is not None:
                        unit_price = override_price

                cost = quantity * unit_price
                if contract_id and contract_id in rebates:
                    cost = cost * (1 - rebates[contract_id] / 100)

                # Label includes metadata if present (e.g. "instance (b2.c4r8)")
                label = metric
                if metadata:
                    meta_str = ", ".join(f"{v}" for v in metadata.values())
                    label = f"{metric} ({meta_str})"

                writer.writerow(
                    [
                        customer_name,
                        cn,
                        project_name,
                        label,
                        f"{quantity:.2f}",
                        unit,
                        round(cost),
                    ]
                )

        # Synthetic cluster billing lines (management fee, setup fees, addons).
        _emit_synthetic_cluster_lines(
            db,
            period_start,
            period_end,
            contract_set,
            prices,
            contract_overrides,
            rebates,
            contract_id_map,
            contract_customer_map,
            writer,
        )

        data_rows = output.getvalue()
        if not data_rows:
            return ""

        report = io.StringIO()
        report_writer = csv.writer(report, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        report_writer.writerow(BILLING_CSV_HEADER)
        report.write(data_rows)
        return UTF8_BOM + report.getvalue()
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
    result = re.sub(r'[^\w\-.]', '_', result)
    return result


# --- Delivery methods ---


def encode_billing_csv(content: str) -> bytes:
    """Encode generated billing CSV text without altering its UTF-8 BOM."""
    return content.encode("utf-8")


async def deliver_webdav(
    url: str, username: str, password: str, filename: str, content: str
) -> None:
    """Upload a file to a WebDAV endpoint.

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
                full_url, resp.status_code, body or "(empty body)",
            )
            raise RuntimeError(
                f"WebDAV PUT returned HTTP {resp.status_code}: "
                f"{body or '(empty body)'}"
            )
    logger.info("Delivered %s to WebDAV (HTTP %d): %s", filename, resp.status_code, url)


async def deliver_email(recipient: str, subject: str, filename: str, content: str) -> None:
    """Send a billing CSV as an email attachment."""
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
    """Parse delivery config JSON and decrypt any encrypted password."""
    config = json.loads(delivery_config_json)
    if "password" in config and config["password"]:
        try:
            config["password"] = decrypt_value(config["password"])
        except Exception:
            logger.warning("Failed to decrypt password, using as-is")
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
                settings.database_url, settings.openstack_cloud,
                [cn], period_start, period_end,
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
            settings.database_url, settings.openstack_cloud,
            contract_numbers, period_start, period_end,
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
    """Execute a billing job: generate CSV(s) and deliver."""
    settings = get_settings()
    period_start, period_end = get_billing_period(year, month)

    run = BillingJobRun(
        billing_job_id=job.id,
        billing_period_start=period_start,
        billing_period_end=period_end,
        status="running",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    try:
        # Resolve contracts (sync query in thread)
        sync_url = settings.database_url.replace("+asyncpg", "")
        if sync_url.startswith("postgresql://"):
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        engine = create_engine(sync_url)
        sf = sessionmaker(bind=engine)

        def _resolve():
            db = sf()
            try:
                return resolve_contract_numbers(db, job, settings.admin_users)
            finally:
                db.close()

        contract_numbers = await asyncio.to_thread(_resolve)
        engine.dispose()

        if not contract_numbers:
            run.status = "success"
            run.completed_at = datetime.utcnow()
            run.files_delivered = 0
            await session.commit()
            return run

        config = _decrypt_config(job.delivery_config)
        files_delivered = await generate_and_deliver(
            settings,
            contract_numbers,
            job.delivery_method,
            config,
            job.filename_template,
            job.per_contract,
            period_start,
            period_end,
        )

        run.status = "success"
        run.files_delivered = files_delivered

    except Exception as e:
        logger.exception("Billing job %d failed", job.id)
        run.status = "error"
        run.error_message = str(e)[:500]

    run.completed_at = datetime.utcnow()
    await session.commit()
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
    """Find and execute all billing jobs that are due now."""
    now = datetime.utcnow()
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
        if existing.scalar_one_or_none():
            continue

        logger.info("Executing due billing job %d: %s", job.id, job.name)
        run = await execute_job(session, job)
        runs.append(run)

    return runs
