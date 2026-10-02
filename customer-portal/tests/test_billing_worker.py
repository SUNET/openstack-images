"""PostgreSQL lifecycle tests for the durable billing worker."""

import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import billing_worker
from app.billing_report_state import requeue_failed_report
from app.models import (
    BillingJob,
    BillingJobRun,
    BillingReport,
    BillingReportOutput,
    BillingReportShard,
)
from tests.test_migration_015 import GitOpsDatabase
from tests.test_migration_015 import gitops_database as gitops_database
from tests.test_migration_015 import gitops_postgres as gitops_postgres


def _report(report_id: str, status: str = "queued") -> BillingReport:
    return BillingReport(
        id=report_id,
        requested_by_sub="admin@test",
        status=status,
        billing_period_start=datetime(2026, 9, 1),
        billing_period_end=datetime(2026, 10, 1),
        contract_numbers_json='["CO-001"]',
        filename_template="billing.csv",
        per_contract=False,
        progress_current=0,
        progress_total=0,
    )


@pytest.mark.asyncio
async def test_worker_completes_queued_report_and_resumes_running_report(
    gitops_database: GitOpsDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_url = gitops_database.sync_url.render_as_string(hide_password=False)
    settings = SimpleNamespace(database_url=sync_url, openstack_cloud="test")

    def complete(database_url: str, cloud_name: str, report_id: str) -> None:
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE billing_report SET status = 'succeeded', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ),
                    {"id": report_id},
                )
        finally:
            engine.dispose()

    monkeypatch.setattr(billing_worker, "process_report", complete)
    try:
        async with sessions() as session:
            session.add_all([_report("queued"), _report("running", "running")])
            await session.commit()

        assert await billing_worker.run_one(sessions, settings) is True
        assert await billing_worker.run_one(sessions, settings) is True
        assert await billing_worker.run_one(sessions, settings) is False

        async with sessions() as session:
            reports = (
                await session.execute(select(BillingReport).order_by(BillingReport.id))
            ).scalars().all()
            assert [report.status for report in reports] == ["succeeded", "succeeded"]
            assert all(report.completed_at is not None for report in reports)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_worker_reclaims_expired_artifact_and_checkpoints(
    gitops_database: GitOpsDatabase,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            report = _report("expired", "succeeded")
            report.delivery_method = "email"
            report.input_snapshot_json = '{"version":1}'
            report.expires_at = billing_worker._utc_now() - timedelta(minutes=1)
            report.shards.append(
                BillingReportShard(
                    metric="instance",
                    project_id="project-1",
                    window_start=datetime(2026, 9, 1),
                    window_end=datetime(2026, 9, 8),
                    status="success",
                    usage_json="[]",
                )
            )
            report.outputs.append(
                BillingReportOutput(
                    filename="billing.csv",
                    media_type="text/csv",
                    content=b"artifact",
                    sha256="a" * 64,
                    size=8,
                    status="sent",
                    delivered_at=billing_worker._utc_now() - timedelta(days=1),
                )
            )
            session.add(report)
            await session.commit()

        async with sessions() as session:
            persisted = await session.get(BillingReport, "expired")
            assert persisted is not None
            assert persisted.result_content is None
            assert persisted.expires_at is not None
            assert persisted.expires_at < billing_worker._utc_now()
            expired_ids = (
                await session.execute(
                    select(BillingReport.id).where(
                        BillingReport.expires_at <= billing_worker._utc_now(),
                        BillingReport.status == "succeeded",
                    )
                )
            ).scalars().all()
            assert expired_ids == ["expired"]

        assert await billing_worker.purge_expired(sessions) == 1

        async with sessions() as session:
            report = await session.get(BillingReport, "expired")
            assert report is not None
            assert report.status == "expired"
            assert report.result_content is None
            assert report.input_snapshot_json is None
            shard_count = await session.scalar(
                select(func.count()).select_from(BillingReportShard)
            )
            assert shard_count == 0
            output_count = await session.scalar(
                select(func.count()).select_from(BillingReportOutput)
            )
            assert output_count == 0
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_advisory_lock_fences_concurrent_workers(
    gitops_database: GitOpsDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_url = gitops_database.sync_url.render_as_string(hide_password=False)
    settings = SimpleNamespace(database_url=sync_url, openstack_cloud="test")
    started = threading.Event()
    release = threading.Event()
    first = None

    def complete(database_url: str, cloud_name: str, report_id: str) -> None:
        started.set()
        assert release.wait(timeout=5)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE billing_report SET status = 'succeeded', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ),
                    {"id": report_id},
                )
        finally:
            engine.dispose()

    monkeypatch.setattr(billing_worker, "process_report", complete)
    try:
        async with sessions() as session:
            session.add(_report("concurrent"))
            await session.commit()

        first = asyncio.create_task(billing_worker.run_one(sessions, settings))
        assert await asyncio.to_thread(started.wait, 5)
        assert await billing_worker.run_one(sessions, settings) is False
        release.set()
        assert await first is True
    finally:
        release.set()
        if first is not None:
            await first
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_worker_failure_marks_linked_run_without_exposing_error(
    gitops_database: GitOpsDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    settings = SimpleNamespace(
        database_url=gitops_database.sync_url.render_as_string(
            hide_password=False
        ),
        openstack_cloud="test",
    )

    def fail(*args) -> None:
        raise RuntimeError("password=not-for-status")

    monkeypatch.setattr(billing_worker, "process_report", fail)
    try:
        async with sessions() as session:
            job = BillingJob(
                name="Monthly",
                owner_sub="admin@test",
                all_contracts=True,
                schedule="0 0 1 * *",
                delivery_method="email",
                delivery_config='{"recipient":"billing@example.test"}',
                filename_template="billing.csv",
                per_contract=False,
            )
            session.add(job)
            await session.flush()
            run = BillingJobRun(
                billing_job_id=job.id,
                billing_period_start=datetime(2026, 9, 1),
                billing_period_end=datetime(2026, 10, 1),
                status="running",
            )
            session.add(run)
            await session.flush()
            report = _report("linked-failure")
            report.billing_job_run_id = run.id
            report.delivery_method = "email"
            report.delivery_config = '{"recipient":"billing@example.test"}'
            session.add(report)
            await session.commit()
            run_id = run.id

        assert await billing_worker.run_one(sessions, settings) is True

        async with sessions() as session:
            stored_report = await session.get(BillingReport, "linked-failure")
            stored_run = await session.get(BillingJobRun, run_id)
            assert stored_report is not None
            assert stored_report.status == "failed"
            assert "password" not in (stored_report.error_message or "")
            assert stored_report.expires_at is not None
            assert stored_run is not None
            assert stored_run.status == "error"
            assert stored_run.completed_at is not None
            assert stored_run.error_message == "Billing report processing failed"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_worker_failure_handler_does_not_overwrite_success(
    gitops_database: GitOpsDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_url = gitops_database.sync_url.render_as_string(hide_password=False)
    settings = SimpleNamespace(database_url=sync_url, openstack_cloud="test")

    def complete_then_raise(database_url, cloud_name, report_id) -> None:
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE billing_report SET status = 'succeeded', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ),
                    {"id": report_id},
                )
        finally:
            engine.dispose()
        raise RuntimeError("after success")

    monkeypatch.setattr(billing_worker, "process_report", complete_then_raise)
    try:
        async with sessions() as session:
            session.add(_report("success-before-exception"))
            await session.commit()

        assert await billing_worker.run_one(sessions, settings) is True

        async with sessions() as session:
            report = await session.get(
                BillingReport, "success-before-exception"
            )
            assert report is not None
            assert report.status == "succeeded"
            assert report.error_message is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_retry_preserves_sent_outputs_and_requeues_linked_run(
    gitops_database: GitOpsDatabase,
) -> None:
    gitops_database.migrate("017")
    async_engine = create_async_engine(gitops_database.async_url)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            job = BillingJob(
                name="Monthly",
                owner_sub="admin@test",
                all_contracts=True,
                schedule="0 0 1 * *",
                delivery_method="email",
                delivery_config='{"recipient":"billing@example.test"}',
                filename_template="billing-{contract}.csv",
                per_contract=True,
            )
            session.add(job)
            await session.flush()
            run = BillingJobRun(
                billing_job_id=job.id,
                billing_period_start=datetime(2026, 9, 1),
                billing_period_end=datetime(2026, 10, 1),
                status="error",
                error_message="Billing report processing failed",
                completed_at=billing_worker._utc_now(),
            )
            session.add(run)
            await session.flush()
            report = _report("retry", "failed")
            report.billing_job_run_id = run.id
            report.delivery_method = "email"
            report.delivery_config = '{"recipient":"billing@example.test"}'
            report.error_message = "Billing report processing failed; retry the report"
            report.completed_at = billing_worker._utc_now()
            report.outputs.extend(
                [
                    BillingReportOutput(
                        filename="sent.csv",
                        media_type="text/csv",
                        content=b"sent",
                        sha256="a" * 64,
                        size=4,
                        status="sent",
                        delivered_at=billing_worker._utc_now(),
                    ),
                    BillingReportOutput(
                        filename="pending.csv",
                        media_type="text/csv",
                        content=b"pending",
                        sha256="b" * 64,
                        size=7,
                        status="pending",
                        error_message="External billing delivery failed",
                    ),
                ]
            )
            session.add(report)
            await session.commit()

            await requeue_failed_report(session, report)
            await session.commit()

        async with sessions() as session:
            report = await session.get(BillingReport, "retry")
            run = await session.get(BillingJobRun, run.id)
            outputs = list(
                (
                    await session.execute(
                        select(BillingReportOutput).order_by(BillingReportOutput.id)
                    )
                ).scalars()
            )
            assert report is not None and report.status == "queued"
            assert report.error_message is None
            assert run is not None and run.status == "running"
            assert run.completed_at is None
            assert [output.status for output in outputs] == ["sent", "pending"]
            assert outputs[1].error_message is None
    finally:
        await async_engine.dispose()


def test_worker_initializes_crypto_before_processing(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        billing_worker,
        "init_crypto",
        lambda secret: calls.append(("crypto", secret)),
    )
    monkeypatch.setattr(
        billing_worker,
        "init_db",
        lambda url: calls.append(("database", url)),
    )
    monkeypatch.setattr(
        billing_worker,
        "run_migrations",
        lambda url: calls.append(("migrations", url)),
    )

    billing_worker.initialize_worker(
        SimpleNamespace(secret_key="secret", database_url="postgresql://db")
    )

    assert calls == [
        ("crypto", "secret"),
        ("database", "postgresql://db"),
        ("migrations", "postgresql://db"),
    ]
