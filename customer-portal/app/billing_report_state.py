"""State transitions shared by billing APIs, schedules, and workers."""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BillingJobRun, BillingReport, BillingReportOutput


async def requeue_failed_report(
    session: AsyncSession,
    report: BillingReport,
) -> BillingJobRun | None:
    """Requeue a failed report while preserving completed delivery checkpoints."""
    if report.status != "failed":
        raise ValueError("Only failed billing reports can be retried")
    report.status = "queued"
    report.error_message = None
    report.started_at = None
    report.completed_at = None
    report.expires_at = None
    await session.execute(
        update(BillingReportOutput)
        .where(
            BillingReportOutput.report_id == report.id,
            BillingReportOutput.status != "sent",
        )
        .values(error_message=None)
    )
    if report.billing_job_run_id is None:
        return None
    run = await session.get(BillingJobRun, report.billing_job_run_id)
    if run is not None:
        run.status = "running"
        run.error_message = None
        run.completed_at = None
    return run
