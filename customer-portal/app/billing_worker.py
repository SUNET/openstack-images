"""Dedicated restart-safe worker for durable billing reports."""

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.billing_reports import ARTIFACT_RETENTION_DAYS, process_report
from app.config import Settings, get_settings
from app.crypto import init_crypto
from app.db import close_db, init_db, run_migrations, session_factory
from app.models import (
    BillingJobRun,
    BillingReport,
    BillingReportOutput,
    BillingReportShard,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return naive UTC for the existing timestamp-without-time-zone schema."""
    return datetime.now(UTC).replace(tzinfo=None)


async def purge_expired(
    sessions: async_sessionmaker[AsyncSession],
) -> int:
    """Reclaim expired artifacts and their no-longer-resumable checkpoints."""
    async with sessions() as session:
        report_ids = (
            await session.execute(
                select(BillingReport.id).where(
                    BillingReport.expires_at <= _utc_now(),
                    BillingReport.status.in_(("succeeded", "failed")),
                )
            )
        ).scalars().all()
        if not report_ids:
            return 0
        await session.execute(
            delete(BillingReportShard).where(
                BillingReportShard.report_id.in_(report_ids)
            )
        )
        await session.execute(
            delete(BillingReportOutput).where(
                BillingReportOutput.report_id.in_(report_ids)
            )
        )
        await session.execute(
            update(BillingReport)
            .where(BillingReport.id.in_(report_ids))
            .values(
                delivery_config=None,
                input_snapshot_json=None,
                result_content=None,
                status="expired",
            )
        )
        await session.commit()
        return len(report_ids)


async def run_one(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> bool:
    """Claim and advance one report while fenced by a session advisory lock."""
    async with sessions() as discovery:
        candidates = (
            await discovery.execute(
                select(BillingReport.id)
                .where(BillingReport.status.in_(("queued", "running")))
                .order_by(BillingReport.created_at, BillingReport.id)
                .limit(30)
            )
        ).scalars().all()

    engine = sessions.kw.get("bind")
    if engine is None:
        raise RuntimeError("Billing worker session factory has no database engine")
    for report_id in candidates:
        async with engine.connect() as raw_lock_connection:
            lock_connection = await raw_lock_connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            acquired = await lock_connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock("
                    "hashtext('billing-report'), hashtext(:report_id))"
                ),
                {"report_id": report_id},
            )
            if not acquired:
                continue
            try:
                async with sessions() as claim_session:
                    report = await claim_session.get(BillingReport, report_id)
                    if report is None or report.status not in {"queued", "running"}:
                        continue
                    if report.status == "queued":
                        report.status = "running"
                        report.started_at = _utc_now()
                        await claim_session.commit()
                try:
                    await asyncio.to_thread(
                        process_report,
                        settings.database_url,
                        settings.openstack_cloud,
                        report_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Billing report failed report=%s type=%s",
                        report_id,
                        type(exc).__name__,
                    )
                    async with sessions() as failure_session:
                        failed = await failure_session.get(BillingReport, report_id)
                        if failed is not None and failed.status == "running":
                            failed.status = "failed"
                            failed.error_message = (
                                "Billing report processing failed; retry the report"
                            )
                            failed.completed_at = _utc_now()
                            failed.expires_at = failed.completed_at + timedelta(
                                days=ARTIFACT_RETENTION_DAYS
                            )
                            if failed.billing_job_run_id is not None:
                                run = await failure_session.get(
                                    BillingJobRun, failed.billing_job_run_id
                                )
                                if run is not None and run.status == "running":
                                    run.status = "error"
                                    run.error_message = (
                                        "Billing report processing failed"
                                    )
                                    run.completed_at = failed.completed_at
                            await failure_session.commit()
                return True
            finally:
                await lock_connection.scalar(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtext('billing-report'), hashtext(:report_id))"
                    ),
                    {"report_id": report_id},
                )
    return False


async def run_worker(
    stop: asyncio.Event,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    next_cleanup = 0.0
    while not stop.is_set():
        try:
            now = asyncio.get_running_loop().time()
            if now >= next_cleanup:
                reclaimed = await purge_expired(sessions)
                if reclaimed:
                    logger.info("Reclaimed %s expired billing artifacts", reclaimed)
                next_cleanup = now + 3600
            worked = await run_one(sessions, settings)
        except Exception as exc:
            logger.error("Billing worker dependency failure type=%s", type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass


def initialize_worker(settings: Settings) -> None:
    """Initialize cryptography, database access, and schema before polling."""
    init_crypto(settings.secret_key)
    init_db(settings.database_url)
    run_migrations(settings.database_url)


async def _main() -> None:
    settings = get_settings()
    initialize_worker(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        await run_worker(stop, session_factory(), settings)
    finally:
        await close_db()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
