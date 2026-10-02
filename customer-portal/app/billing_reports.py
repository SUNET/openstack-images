"""Durable, checkpointed billing report generation and delivery."""

import asyncio
import hashlib
import io
import json
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import openstack
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.billing_runner import (
    GnocchiQueryTimeout,
    GnocchiShardTooLarge,
    _decrypt_config,
    _deliver,
    _query_gnocchi_usage,
    capture_billing_input_snapshot,
    encode_billing_csv,
    load_billing_input_snapshot,
    render_billing_csv,
    resolve_template,
    serialize_billing_input_snapshot,
)
from app.models import (
    BillingJobRun,
    BillingReport,
    BillingReportOutput,
    BillingReportShard,
)

INITIAL_SHARD_DAYS = 7
MINIMUM_SHARD_SECONDS = 3600
ARTIFACT_RETENTION_DAYS = 7

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return naive UTC for the existing timestamp-without-time-zone schema."""
    return datetime.now(UTC).replace(tzinfo=None)


def _sync_url(database_url: str) -> str:
    url = database_url.replace("+asyncpg", "")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _windows(start: datetime, end: datetime):
    current = start
    while current < end:
        following = min(current + timedelta(days=INITIAL_SHARD_DAYS), end)
        yield current, following
        current = following


def _json_usage(usage: list[dict]) -> str:
    serializable = []
    for entry in usage:
        item = dict(entry)
        item["hours"] = str(item["hours"])
        item["size_months"] = str(item["size_months"])
        serializable.append(item)
    return json.dumps(serializable, sort_keys=True, separators=(",", ":"))


def _merge_usage(shards: list[BillingReportShard]) -> dict[str, list[dict]]:
    merged: dict[tuple, dict] = {}
    for shard in shards:
        for entry in json.loads(shard.usage_json or "[]"):
            metadata = entry.get("metadata", {})
            key = (
                shard.metric,
                entry["project_id"],
                tuple(sorted(metadata.items())),
            )
            target = merged.setdefault(
                key,
                {
                    "project_id": entry["project_id"],
                    "metric": entry["metric"],
                    "metadata": metadata,
                    "hours": Decimal(0),
                    "size_months": Decimal(0),
                },
            )
            target["hours"] += Decimal(entry["hours"])
            target["size_months"] += Decimal(entry["size_months"])
    by_metric: dict[str, list[dict]] = {}
    for (metric, _, _), entry in merged.items():
        by_metric.setdefault(metric, []).append(entry)
    return by_metric


def _update_progress(db, report: BillingReport) -> None:
    report.progress_current = db.scalar(
        select(func.count()).select_from(BillingReportShard).where(
            BillingReportShard.report_id == report.id,
            BillingReportShard.status == "success",
        )
    ) or 0
    report.progress_total = db.scalar(
        select(func.count()).select_from(BillingReportShard).where(
            BillingReportShard.report_id == report.id,
            BillingReportShard.status != "split",
        )
    ) or 0


def _split_shard(db, shard: BillingReportShard) -> None:
    duration = int((shard.window_end - shard.window_start).total_seconds())
    hours = duration // MINIMUM_SHARD_SECONDS
    if hours <= 1:
        raise GnocchiShardTooLarge(
            f"Minimum Gnocchi shard remains too large for {shard.metric} "
            f"in project {shard.project_id}"
        )
    midpoint = shard.window_start + timedelta(hours=max(1, hours // 2))
    for start, end in (
        (shard.window_start, midpoint),
        (midpoint, shard.window_end),
    ):
        db.add(
            BillingReportShard(
                report_id=shard.report_id,
                metric=shard.metric,
                project_id=shard.project_id,
                window_start=start,
                window_end=end,
                status="pending",
            )
        )
    shard.status = "split"
    shard.error_message = None


def _artifacts(
    snapshot: dict,
    usage: dict[str, list[dict]],
) -> list[tuple[str, str, bytes]]:
    """Render immutable CSV outputs solely from the frozen snapshot and usage."""
    contracts = [contract["number"] for contract in snapshot["contracts"]]
    artifact = snapshot["artifact"]
    template_vars = snapshot["filename_variables"]
    if not artifact["per_contract"]:
        content = render_billing_csv(snapshot, usage, contracts)
        if not content.strip():
            raise ValueError("Billing report is empty")
        return [
            (
                resolve_template(artifact["filename_template"], **template_vars),
                "text/csv; charset=utf-8",
                encode_billing_csv(content),
            )
        ]

    outputs = []
    filenames: set[str] = set()
    for contract in contracts:
        content = render_billing_csv(snapshot, usage, [contract])
        if not content.strip():
            continue
        template = artifact["filename_template"]
        if "{contract}" not in template:
            stem, separator, suffix = template.rpartition(".")
            template = (
                f"{stem}-{{contract}}.{suffix}"
                if separator
                else f"{template}-{{contract}}"
            )
        filename = resolve_template(template, **template_vars, contract=contract)
        if filename in filenames:
            raise ValueError("Per-contract filename template produced duplicates")
        filenames.add(filename)
        outputs.append(
            (filename, "text/csv; charset=utf-8", encode_billing_csv(content))
        )
    if not outputs:
        raise ValueError("Billing report is empty")
    return outputs


def _download_artifact(
    snapshot: dict, outputs: list[tuple[str, str, bytes]]
) -> tuple[str, str, bytes]:
    """Publish one deterministic download from already-rendered outputs."""
    if not snapshot["artifact"]["per_contract"]:
        return outputs[0]

    template_vars = snapshot["filename_variables"]
    artifact_date = datetime.fromisoformat(template_vars["date"])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for filename, _media_type, content in outputs:
            entry = zipfile.ZipInfo(
                filename,
                date_time=(
                    artifact_date.year,
                    artifact_date.month,
                    artifact_date.day,
                    0,
                    0,
                    0,
                ),
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(entry, content)
    return (
        f"billing-{template_vars['year']}-{template_vars['month']}.zip",
        "application/zip",
        archive.getvalue(),
    )


def process_report(database_url: str, cloud_name: str, report_id: str) -> bool:
    """Resume generation, checkpoint outputs, and deliver each pending output.

    WebDAV retries replace the same filename and are idempotent. SMTP is
    at-least-once: a crash after the SMTP server accepts a message but before
    the sent checkpoint commits can cause that attachment to be sent again.
    """
    engine = create_engine(
        _sync_url(database_url),
        isolation_level="REPEATABLE READ",
    )
    lock_engine = create_engine(_sync_url(database_url))
    lock_connection = lock_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    )
    acquired = lock_connection.scalar(
        text(
            "SELECT pg_try_advisory_lock("
            "hashtext('billing-report-processor'), hashtext(:report_id))"
        ),
        {"report_id": report_id},
    )
    if not acquired:
        lock_connection.close()
        lock_engine.dispose()
        engine.dispose()
        return False
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as db:
            report = db.get(BillingReport, report_id)
            if report is None:
                return False
            conn = None
            if report.input_snapshot_json is None:
                if report.shards:
                    raise ValueError("Billing report has shards without an input snapshot")
                conn = openstack.connect(cloud=cloud_name)
                snapshot = capture_billing_input_snapshot(
                    db,
                    conn,
                    json.loads(report.contract_numbers_json),
                    report.billing_period_start,
                    report.billing_period_end,
                    filename_template=report.filename_template,
                    per_contract=report.per_contract,
                )
                report.input_snapshot_json = serialize_billing_input_snapshot(snapshot)
                db.commit()
            else:
                snapshot = load_billing_input_snapshot(report.input_snapshot_json)

            if not report.shards:
                snapshot_start = datetime.fromisoformat(
                    snapshot["period"]["start"]
                ).replace(tzinfo=None)
                snapshot_end = datetime.fromisoformat(
                    snapshot["period"]["end"]
                ).replace(tzinfo=None)
                for product in snapshot["query_plan"]:
                    for project in snapshot["projects"]:
                        for start, end in _windows(snapshot_start, snapshot_end):
                            db.add(
                                BillingReportShard(
                                    report_id=report.id,
                                    metric=product["metric"],
                                    project_id=project["id"],
                                    window_start=start,
                                    window_end=end,
                                    status="pending",
                                )
                            )
                db.flush()
                _update_progress(db, report)
                db.commit()

            period_start = datetime.fromisoformat(snapshot["period"]["start"])
            period_end = datetime.fromisoformat(snapshot["period"]["end"])
            normalization = Decimal(str((period_end - period_start).total_seconds()))
            products = {
                product["metric"]: product for product in snapshot["query_plan"]
            }
            while True:
                shard = db.scalar(
                    select(BillingReportShard)
                    .where(
                        BillingReportShard.report_id == report.id,
                        BillingReportShard.status == "pending",
                    )
                    .order_by(BillingReportShard.id)
                    .limit(1)
                )
                if shard is None:
                    break
                product = products[shard.metric]
                if conn is None:
                    conn = openstack.connect(cloud=cloud_name)
                try:
                    usage = _query_gnocchi_usage(
                        conn,
                        shard.window_start,
                        shard.window_end,
                        product["resource_type"],
                        product["source_metric"],
                        product["metadata_fields"],
                        [shard.project_id],
                        aggregate_across_resources=(
                            product["aggregation"] == "additive_size"
                        ),
                        normalization_period_seconds=normalization,
                    )
                except (GnocchiQueryTimeout, GnocchiShardTooLarge):
                    _split_shard(db, shard)
                else:
                    shard.usage_json = _json_usage(usage)
                    shard.status = "success"
                    shard.error_message = None
                _update_progress(db, report)
                db.commit()

            outputs = list(
                db.scalars(
                    select(BillingReportOutput)
                    .where(BillingReportOutput.report_id == report.id)
                    .order_by(BillingReportOutput.id)
                )
            )
            if not outputs:
                successful = db.scalars(
                    select(BillingReportShard).where(
                        BillingReportShard.report_id == report.id,
                        BillingReportShard.status == "success",
                    )
                ).all()
                usage = _merge_usage(list(successful))
                rendered = _artifacts(snapshot, usage)
                output_status = "pending" if report.delivery_method else "ready"
                for filename, media_type, content in rendered:
                    db.add(
                        BillingReportOutput(
                            report_id=report.id,
                            filename=filename,
                            media_type=media_type,
                            content=content,
                            sha256=hashlib.sha256(content).hexdigest(),
                            size=len(content),
                            status=output_status,
                        )
                    )
                if report.delivery_method is None:
                    filename, media_type, content = _download_artifact(
                        snapshot, rendered
                    )
                    report.result_filename = filename
                    report.result_media_type = media_type
                    report.result_content = content
                    report.result_sha256 = hashlib.sha256(content).hexdigest()
                    report.result_size = len(content)
                db.commit()
                outputs = list(
                    db.scalars(
                        select(BillingReportOutput)
                        .where(BillingReportOutput.report_id == report.id)
                        .order_by(BillingReportOutput.id)
                    )
                )

            if report.delivery_method is not None:
                if report.delivery_config is None:
                    raise ValueError("Billing delivery configuration is missing")
                config = _decrypt_config(report.delivery_config)
                for output in outputs:
                    if output.status == "sent":
                        continue
                    if report.delivery_method == "email":
                        logger.info(
                            "Sending billing output with at-least-once SMTP "
                            "semantics report=%s output=%s",
                            report.id,
                            output.id,
                        )
                    try:
                        asyncio.run(
                            _deliver(
                                report.delivery_method,
                                config,
                                output.filename,
                                output.content.decode("utf-8"),
                            )
                        )
                    except Exception:
                        output.error_message = "External billing delivery failed"
                        db.commit()
                        raise
                    output.status = "sent"
                    output.delivered_at = _utc_now()
                    output.error_message = None
                    db.commit()

            report.status = "succeeded"
            report.completed_at = _utc_now()
            report.expires_at = _utc_now() + timedelta(days=ARTIFACT_RETENTION_DAYS)
            report.delivery_config = None
            report.progress_current = report.progress_total
            if report.billing_job_run_id is not None:
                run = db.get(BillingJobRun, report.billing_job_run_id)
                if run is not None:
                    run.status = "success"
                    run.files_delivered = sum(
                        output.status == "sent" for output in outputs
                    )
                    run.error_message = None
                    run.completed_at = report.completed_at
            db.commit()
            return True
    finally:
        lock_connection.scalar(
            text(
                "SELECT pg_advisory_unlock("
                "hashtext('billing-report-processor'), hashtext(:report_id))"
            ),
            {"report_id": report_id},
        )
        lock_connection.close()
        lock_engine.dispose()
        engine.dispose()
