"""Tests for durable ad-hoc billing report enqueueing."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.models import BillingReport
from app.routers import billing
from app.schemas import RunOnceDownloadRequest, RunOnceRequest


def _request(per_contract: bool = False) -> RunOnceDownloadRequest:
    return RunOnceDownloadRequest(
        all_contracts=True,
        filename_template="billing-{year}-{month}.csv",
        per_contract=per_contract,
        year=2026,
        month=7,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"year": 2026, "month": None},
        {"year": None, "month": 7},
        {"year": 2026, "month": 13},
    ],
)
def test_download_period_must_be_complete_and_valid(values) -> None:
    with pytest.raises(ValidationError):
        RunOnceDownloadRequest(all_contracts=True, **values)


def _mock_download_dependencies(monkeypatch):
    monkeypatch.setattr(
        billing,
        "get_settings",
        lambda: SimpleNamespace(admin_users=["admin@test"]),
    )
    monkeypatch.setattr(
        billing,
        "_resolve_run_once_contracts",
        AsyncMock(return_value=["CO-001", "CO-002"]),
    )
    monkeypatch.setattr(billing, "audit_log", lambda *args, **kwargs: None)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_report_enqueue_snapshots_scope_and_returns_metadata(monkeypatch) -> None:
    _mock_download_dependencies(monkeypatch)
    session = SimpleNamespace(
        add=lambda report: None,
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    async def refresh(report):
        report.created_at = datetime(2026, 8, 1)

    session.refresh.side_effect = refresh

    response = await billing.create_report(
        _request(),
        {"sub": "admin@test"},
        session,
    )

    assert response.status == "queued"
    assert response.progress_current == 0
    assert response.progress_total == 0
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_validates_encrypts_and_returns_queued_report(monkeypatch) -> None:
    _mock_download_dependencies(monkeypatch)
    monkeypatch.setattr(
        billing,
        "_validate_delivery_config",
        Mock(return_value={"url": "https://dav.example", "password": "secret"}),
    )
    monkeypatch.setattr(
        billing,
        "_encrypt_delivery_config",
        Mock(return_value='{"password":"encrypted"}'),
    )
    queued = BillingReport(
        id="queued-report",
        requested_by_sub="admin@test",
        status="queued",
        billing_period_start=datetime(2026, 7, 1),
        billing_period_end=datetime(2026, 8, 1),
        contract_numbers_json='["CO-001"]',
        filename_template="billing.csv",
        per_contract=False,
        delivery_method="webdav",
        delivery_config='{"password":"encrypted"}',
        progress_current=0,
        progress_total=0,
    )
    enqueue = AsyncMock(return_value=queued)
    monkeypatch.setattr(billing, "_enqueue_report", enqueue)
    request = RunOnceRequest(
        all_contracts=True,
        delivery_method="webdav",
        delivery_config={"url": "https://dav.example", "password": "secret"},
        year=2026,
        month=7,
    )

    response = await billing.run_once(
        request,
        {"sub": "admin@test"},
        SimpleNamespace(),
    )

    assert response.status == "queued"
    assert response.report_id == "queued-report"
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["delivery_config"] == '{"password":"encrypted"}'


def test_old_synchronous_download_handler_is_removed() -> None:
    assert not hasattr(billing, "download_run_once")


@pytest.mark.asyncio
async def test_report_download_requires_owner_and_ready_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        billing,
        "get_settings",
        lambda: SimpleNamespace(admin_users=[]),
    )
    monkeypatch.setattr(billing, "audit_log", lambda *args, **kwargs: None)
    report = BillingReport(
        id="report-1",
        requested_by_sub="owner@test",
        status="succeeded",
        billing_period_start=datetime(2026, 7, 1),
        billing_period_end=datetime(2026, 8, 1),
        contract_numbers_json='["CO-001"]',
        filename_template="billing.csv",
        per_contract=False,
        progress_current=1,
        progress_total=1,
        result_filename="billing.csv",
        result_media_type="text/csv; charset=utf-8",
        result_content=b"report",
    )
    accessible = SimpleNamespace(scalars=lambda: ["CO-001"])
    session = SimpleNamespace(
        get=AsyncMock(return_value=report),
        execute=AsyncMock(return_value=accessible),
    )

    response = await billing.download_report(
        report.id,
        {"sub": "owner@test"},
        session,
    )

    assert response.body == b"report"
    assert response.headers["cache-control"] == "private, no-store"
    with pytest.raises(billing.HTTPException) as denied:
        await billing.download_report(
            report.id,
            {"sub": "other@test"},
            session,
        )
    assert denied.value.status_code == 404

    session.execute.return_value = SimpleNamespace(scalars=lambda: [])
    with pytest.raises(billing.HTTPException) as revoked:
        await billing.download_report(
            report.id,
            {"sub": "owner@test"},
            session,
        )
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
async def test_recent_reports_are_owner_scoped_without_artifact_load(monkeypatch) -> None:
    monkeypatch.setattr(
        billing,
        "get_settings",
        lambda: SimpleNamespace(admin_users=["admin@test"]),
    )
    report = BillingReport(
        id="report-1",
        requested_by_sub="admin@test",
        status="queued",
        billing_period_start=datetime(2026, 7, 1),
        billing_period_end=datetime(2026, 8, 1),
        contract_numbers_json='["CO-001"]',
        filename_template="billing.csv",
        per_contract=False,
        progress_current=0,
        progress_total=0,
        created_at=datetime(2026, 8, 1),
    )
    result = SimpleNamespace(scalars=lambda: [report])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    reports = await billing.list_reports({"sub": "admin@test"}, session)

    assert [item.id for item in reports] == ["report-1"]
    statement = session.execute.await_args.args[0]
    assert "result_content" not in str(statement)


@pytest.mark.asyncio
async def test_failed_report_retry_uses_checkpoint_preserving_transition(
    monkeypatch,
) -> None:
    report = BillingReport(
        id="report-1",
        requested_by_sub="owner@test",
        status="failed",
        billing_period_start=datetime(2026, 7, 1),
        billing_period_end=datetime(2026, 8, 1),
        contract_numbers_json='["CO-001"]',
        filename_template="billing.csv",
        per_contract=False,
        progress_current=1,
        progress_total=2,
        created_at=datetime(2026, 8, 1),
    )
    owned = AsyncMock(return_value=report)

    async def requeue(session, selected):
        assert selected is report
        selected.status = "queued"

    monkeypatch.setattr(billing, "_owned_report", owned)
    monkeypatch.setattr(billing, "requeue_failed_report", requeue)
    monkeypatch.setattr(billing, "audit_log", lambda *args, **kwargs: None)
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    response = await billing.retry_report(
        report.id,
        {"sub": "owner@test"},
        session,
    )

    assert response.status == "queued"
    session.commit.assert_awaited_once()
