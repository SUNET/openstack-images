"""Unit tests for billing generation failure handling."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import billing_runner
from app.billing_runner import (
    BillingGenerationError,
    _query_gnocchi_usage,
    generate_and_deliver,
)


def test_gnocchi_http_error_fails_billing(monkeypatch) -> None:
    response = SimpleNamespace(status_code=404)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    with pytest.raises(BillingGenerationError, match="returned HTTP 404"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "instance",
            "cpu",
            ["flavor_name"],
        )


def test_gnocchi_exception_fails_billing(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(httpx, "post", fail)

    with pytest.raises(BillingGenerationError, match="Failed to query Gnocchi"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "volume",
            "volume.size",
            ["volume_type"],
        )


@pytest.mark.asyncio
async def test_empty_combined_report_is_not_delivered(monkeypatch) -> None:
    monkeypatch.setattr(billing_runner, "generate_billing_csv", lambda *args: "")
    deliver = AsyncMock()
    monkeypatch.setattr(billing_runner, "_deliver", deliver)

    settings = SimpleNamespace(database_url="postgresql://unused", openstack_cloud="openstack")
    with pytest.raises(BillingGenerationError, match="refusing to deliver"):
        await generate_and_deliver(
            settings,
            ["CO-001"],
            "webdav",
            {"url": "https://dav.example.invalid"},
            "billing-{year}-{month}.csv",
            False,
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
        )

    deliver.assert_not_awaited()
