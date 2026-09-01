"""Tests for direct ad-hoc billing downloads."""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.routers import billing
from app.schemas import RunOnceDownloadRequest


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


def _mock_download_dependencies(monkeypatch, files):
    async def generate_files(*args, **kwargs):
        for item in files:
            yield item

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
    monkeypatch.setattr(
        billing,
        "iter_billing_files",
        generate_files,
    )
    monkeypatch.setattr(billing, "audit_log", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_direct_download_returns_utf8_csv(monkeypatch) -> None:
    content = "\ufeff# Customer;Quantity;Unit\r\nDatatjänst;1.00;hour\r\n"
    _mock_download_dependencies(
        monkeypatch,
        [("fakturering-2026-07.csv", content)],
    )

    response = await billing.download_run_once(
        _request(),
        {"sub": "admin@test"},
        SimpleNamespace(),
    )

    assert response.media_type == "text/csv; charset=utf-8"
    assert response.body.startswith(b"\xef\xbb\xbf")
    assert b"Datatj\xc3\xa4nst" in response.body
    assert "fakturering-2026-07.csv" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_per_contract_download_returns_zip(monkeypatch) -> None:
    files = [
        ("billing-CO-001.csv", "\ufefffirst"),
        ("billing-CO-002.csv", "\ufeffsecond"),
    ]
    _mock_download_dependencies(monkeypatch, files)

    response = await billing.download_run_once(
        _request(per_contract=True),
        {"sub": "admin@test"},
        SimpleNamespace(),
    )

    assert response.media_type == "application/zip"
    assert "billing-2026-07.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.namelist() == ["billing-CO-001.csv", "billing-CO-002.csv"]
        assert archive.read("billing-CO-001.csv") == b"\xef\xbb\xbffirst"
        assert archive.read("billing-CO-002.csv") == b"\xef\xbb\xbfsecond"
