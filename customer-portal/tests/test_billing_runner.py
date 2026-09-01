"""Unit tests for billing generation and failure handling."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import billing_runner
from app.billing_runner import (
    BILLING_GRANULARITY_SECONDS,
    GNOCCHI_METRIC_METADATA_FIELDS,
    GNOCCHI_METRIC_SOURCES,
    GNOCCHI_PRODUCT_REGISTRY,
    BillingGenerationError,
    _get_cinder_volume_type_names,
    _get_project_contracts,
    _query_gnocchi_usage,
    _resolve_cinder_volume_type,
    generate_and_deliver,
    generate_billing_csv,
    get_billing_period,
)


def _response(groups: list[dict], status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        content=b"response",
        json=lambda: groups,
    )


def _group(
    resource_id: str,
    metadata: dict[str, str],
    measures: list[list],
    project_id: str = "project-1",
) -> dict:
    return {
        "group": {
            "project_id": project_id,
            "id": resource_id,
            "original_resource_id": resource_id,
            **metadata,
        },
        "measures": {"measures": {"aggregated": measures}},
    }


def _resource(resource_id: str, *metric_names: str) -> dict:
    return {
        "id": resource_id,
        "metrics": {name: f"metric-{resource_id}-{name}" for name in metric_names},
    }


def test_default_billing_period_is_previous_calendar_month(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 13, 42, 16, 722415, tzinfo=tz)

    monkeypatch.setattr(billing_runner, "datetime", FixedDateTime)

    start, end = get_billing_period()

    assert start == datetime(2026, 8, 1)
    assert end == datetime(2026, 9, 1)


def test_gnocchi_http_error_fails_billing(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response([], status_code=500),
    )

    with pytest.raises(BillingGenerationError, match="returned HTTP 500"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "instance",
            "cpu",
            ["flavor_name"],
            ["project-1"],
        )


def test_gnocchi_404_means_project_has_no_usage(monkeypatch) -> None:
    responses = iter([_response([], status_code=404), _response([])])
    requests = []

    def respond(*args, **kwargs):
        requests.append((args[0], kwargs))
        return next(responses)

    monkeypatch.setattr(httpx, "post", respond)

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "volume",
        "volume.size",
        ["volume_type"],
        ["project-1"],
    )

    assert usage == []
    assert requests[1][0].endswith("/v1/search/resource/volume")
    assert requests[1][1] == {
        "params": [("limit", "100"), ("sort", "id:asc")],
        "json": {
            "and": [
                {"=": {"project_id": "project-1"}},
                {"<": {"started_at": "2026-08-01T00:00:00+00:00"}},
                {
                    "or": [
                        {">": {"ended_at": "2026-07-01T00:00:00+00:00"}},
                        {"=": {"ended_at": None}},
                    ]
                },
            ]
        },
        "headers": {"X-Auth-Token": "test-token"},
        "timeout": 60,
    }


def test_gnocchi_404_with_expected_family_resource_fails_billing(monkeypatch) -> None:
    responses = iter(
        [
            _response([], status_code=404),
            _response([_resource("volume-1", "volume")]),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    with pytest.raises(BillingGenerationError, match="returned HTTP 404"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "volume",
            "volume.size",
            ["volume_type"],
            ["project-1"],
        )


def test_volume_404_ignores_snapshot_and_backup_resources(monkeypatch) -> None:
    responses = iter(
        [
            _response([], status_code=404),
            _response(
                [
                    _resource("backup-1", "backup.size"),
                    _resource("snapshot-1", "volume.snapshot.size"),
                ]
            ),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "volume",
        "volume.size",
        ["volume_type"],
        ["project-1"],
    )

    assert usage == []


@pytest.mark.parametrize(
    ("metric_name", "other_metrics"),
    [
        ("volume.snapshot.size", ("volume", "backup.size")),
        ("volume.backup.size", ("volume.size", "snapshot.size")),
    ],
)
def test_snapshot_and_backup_404s_ignore_other_volume_families(
    monkeypatch, metric_name, other_metrics
) -> None:
    responses = iter(
        [
            _response([], status_code=404),
            _response(
                [
                    _resource("resource-1", other_metrics[0]),
                    _resource("resource-2", other_metrics[1]),
                ]
            ),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "volume",
        metric_name,
        [],
        ["project-1"],
    )

    assert usage == []


def test_non_cinder_404_with_any_resource_fails_billing(monkeypatch) -> None:
    responses = iter(
        [
            _response([], status_code=404),
            _response([_resource("instance-1", "unrelated")]),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    with pytest.raises(BillingGenerationError, match="returned HTTP 404"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "instance",
            "cpu",
            ["flavor_name"],
            ["project-1"],
        )


def test_gnocchi_404_finds_expected_family_on_later_resource_page(monkeypatch) -> None:
    monkeypatch.setattr(billing_runner, "GNOCCHI_RESOURCE_PAGE_SIZE", 2)
    responses = iter(
        [
            _response([], status_code=404),
            _response(
                [
                    _resource("resource-a", "volume"),
                    _resource("resource-b", "backup.size"),
                ]
            ),
            _response([_resource("resource-c", "snapshot.size")]),
        ]
    )
    requests = []

    def respond(*args, **kwargs):
        requests.append((args[0], kwargs))
        return next(responses)

    monkeypatch.setattr(httpx, "post", respond)

    with pytest.raises(BillingGenerationError, match="returned HTTP 404"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "volume",
            "volume.snapshot.size",
            [],
            ["project-1"],
        )

    assert requests[2][1]["params"] == [
        ("limit", "2"),
        ("sort", "id:asc"),
        ("marker", "resource-b"),
    ]


@pytest.mark.parametrize(
    ("search_pages", "message"),
    [
        ([_response([{"id": "resource-a"}])], "Invalid Gnocchi resource"),
        (
            [
                _response(
                    [
                        _resource("resource-a", "volume"),
                        _resource("resource-b", "volume"),
                    ]
                ),
                _response([_resource("resource-b", "volume")]),
            ],
            "Non-advancing Gnocchi resource marker",
        ),
    ],
    ids=["malformed", "non-advancing"],
)
def test_gnocchi_404_resource_pagination_fails_closed(monkeypatch, search_pages, message) -> None:
    monkeypatch.setattr(billing_runner, "GNOCCHI_RESOURCE_PAGE_SIZE", 2)
    responses = iter([_response([], status_code=404), *search_pages])
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    with pytest.raises(BillingGenerationError, match=message):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "volume",
            "volume.snapshot.size",
            [],
            ["project-1"],
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
            ["project-1"],
        )


def test_gnocchi_product_mappings_and_storage_scaling() -> None:
    assert GNOCCHI_METRIC_SOURCES["instance"] == ("instance", "cpu")
    assert GNOCCHI_METRIC_SOURCES["volume.snapshot.size"] == (
        "volume",
        "volume.snapshot.size",
    )
    assert GNOCCHI_METRIC_SOURCES["volume.backup.size"] == (
        "volume",
        "volume.backup.size",
    )
    assert GNOCCHI_METRIC_METADATA_FIELDS["volume.size"] == {"volume_type"}
    assert GNOCCHI_METRIC_METADATA_FIELDS["volume.snapshot.size"] == set()
    assert GNOCCHI_METRIC_METADATA_FIELDS["volume.backup.size"] == set()
    assert GNOCCHI_PRODUCT_REGISTRY["volume.snapshot.size"]["unit"] == "GB-month"
    assert GNOCCHI_PRODUCT_REGISTRY["volume.backup.size"]["unit"] == "GB-month"
    assert GNOCCHI_PRODUCT_REGISTRY["volume.snapshot.size"]["size_gb_scale"] == Decimal(1)
    assert GNOCCHI_PRODUCT_REGISTRY["volume.backup.size"]["size_gb_scale"] == Decimal(1)
    assert GNOCCHI_PRODUCT_REGISTRY["radosgw.objects.size"]["size_gb_scale"] == Decimal(
        1
    ) / Decimal(10**9)


def test_cinder_volume_type_resolution_accepts_id_or_name_and_rejects_unknown() -> None:
    type_names = {"type-uuid": "rbd1"}

    assert _resolve_cinder_volume_type("type-uuid", type_names) == "rbd1"
    assert _resolve_cinder_volume_type("rbd1", type_names) == "rbd1"
    with pytest.raises(BillingGenerationError, match="unknown or no longer active"):
        _resolve_cinder_volume_type("deleted-type", type_names)


@pytest.mark.parametrize(
    ("volume_types", "message"),
    [
        ([], "no active volume types"),
        ([SimpleNamespace(id=None, name="rbd1")], "without an ID"),
    ],
)
def test_cinder_volume_type_catalog_fails_closed(volume_types, message) -> None:
    connection = SimpleNamespace(block_storage=SimpleNamespace(types=lambda: volume_types))

    with pytest.raises(BillingGenerationError, match=message):
        _get_cinder_volume_type_names(connection)


def test_unsupported_metered_product_fails_closed(monkeypatch) -> None:
    price = SimpleNamespace(
        resource_type="image.size",
        metadata_field=None,
        metadata_value=None,
        unit_price=Decimal("1.00"),
        unit="GB-month",
    )
    project = SimpleNamespace(
        id="project-1",
        name="Example project",
        tags=["contract:CO-001"],
    )
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: [project]),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: [price])
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {"CO-001": 1})
    monkeypatch.setattr(
        billing_runner,
        "_load_contract_customers",
        lambda db: {"CO-001": "Acme"},
    )
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)

    with pytest.raises(BillingGenerationError, match="Unsupported metered"):
        generate_billing_csv(
            "postgresql://unused",
            "openstack",
            ["CO-001"],
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
        )


def test_multiple_project_contract_tags_fail_closed() -> None:
    project = SimpleNamespace(
        id="project-1",
        name="Ambiguous project",
        tags=["contract:CONTRACT-2", "unrelated", "contract:CONTRACT-1"],
    )
    connection = SimpleNamespace(identity=SimpleNamespace(projects=lambda: [project]))

    with pytest.raises(
        BillingGenerationError,
        match=r"Ambiguous project.*CONTRACT-1.*CONTRACT-2",
    ):
        _get_project_contracts(connection)


def test_empty_project_contract_tag_fails_closed() -> None:
    project = SimpleNamespace(
        id="project-1",
        name="Empty contract project",
        tags=["contract:"],
    )
    connection = SimpleNamespace(identity=SimpleNamespace(projects=lambda: [project]))

    with pytest.raises(BillingGenerationError, match=r"Empty contract project.*empty"):
        _get_project_contracts(connection)


def test_gnocchi_usage_counts_each_resource_with_hourly_granularity(monkeypatch) -> None:
    request = {}
    groups = [
        _group(
            "instance-1",
            {"flavor_name": "b2.c1r2"},
            [
                ["2026-07-01T00:00:00+00:00", 3600, 10],
                ["2026-07-01T01:00:00+00:00", 3600, 20],
            ],
        ),
        _group(
            "instance-2",
            {"flavor_name": "b2.c1r2"},
            [["2026-07-01T00:00:00+00:00", 3600, 0]],
        ),
    ]

    def record_post(*args, **kwargs):
        request["url"] = args[0]
        request.update(kwargs)
        return _response(groups)

    monkeypatch.setattr(httpx, "post", record_post)

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "instance",
        "cpu",
        ["flavor_name"],
        ["project-1"],
    )

    assert len(usage) == 1
    assert usage[0]["project_id"] == "project-1"
    assert usage[0]["metadata"] == {"flavor_name": "b2.c1r2"}
    assert usage[0]["hours"] == Decimal(3)
    assert request["url"].endswith("/v1/aggregates")
    assert ("granularity", str(BILLING_GRANULARITY_SECONDS)) in request["params"]
    assert ("use_history", "true") in request["params"]
    assert ("groupby", "original_resource_id") in request["params"]
    assert request["json"] == {
        "resource_type": "instance",
        "search": {"=": {"project_id": "project-1"}},
        "operations": ["aggregate", "sum", ["metric", "cpu", "mean"]],
    }


def test_gnocchi_usage_attributes_resize_hour_to_each_flavor(monkeypatch) -> None:
    groups = [
        _group(
            "instance-1",
            {"flavor_name": "b2.c1r2"},
            [["2026-07-01T00:00:00+00:00", 3600, 10]],
        ),
        _group(
            "instance-1",
            {"flavor_name": "b2.c2r4"},
            [["2026-07-01T00:00:00+00:00", 3600, 20]],
        ),
    ]
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _response(groups))

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "instance",
        "cpu",
        ["flavor_name"],
        ["project-1"],
    )

    assert {(row["metadata"]["flavor_name"], row["hours"]) for row in usage} == {
        ("b2.c1r2", Decimal(1)),
        ("b2.c2r4", Decimal(1)),
    }


def test_gnocchi_usage_sums_size_across_resources(monkeypatch) -> None:
    groups = [
        _group(
            "volume-1",
            {"volume_type": "fast"},
            [["2026-07-01T00:00:00+00:00", 3600, 10]],
        ),
        _group(
            "volume-2",
            {"volume_type": "fast"},
            [["2026-07-01T00:00:00+00:00", 3600, 20]],
        ),
    ]
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _response(groups))

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 7, 1, 2),
        "volume",
        "volume.size",
        ["volume_type"],
        ["project-1"],
    )

    assert len(usage) == 1
    assert usage[0]["project_id"] == "project-1"
    assert usage[0]["metadata"] == {"volume_type": "fast"}
    assert usage[0]["size_months"] == Decimal(15)


def test_gnocchi_usage_omits_empty_groups(monkeypatch) -> None:
    groups = [_group("instance-1", {"flavor_name": "b2.c1r2"}, [])]
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _response(groups))

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="test-token"),
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
        "instance",
        "cpu",
        ["flavor_name"],
        ["project-1"],
    )

    assert usage == []


@pytest.mark.parametrize(
    "measure",
    [
        ["2026-07-01T00:00:00+00:00", 300, 1],
        ["2026-07-01T00:30:00+00:00", 3600, 1],
        ["2026-07-01T00:00:00+00:00", 3600, None],
        ["2026-07-01T00:00:00+00:00", 3600, -1],
    ],
)
def test_gnocchi_usage_rejects_invalid_measure(monkeypatch, measure) -> None:
    groups = [_group("instance-1", {"flavor_name": "b2.c1r2"}, [measure])]
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _response(groups))

    with pytest.raises(BillingGenerationError, match="Invalid Gnocchi"):
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="test-token"),
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
            "instance",
            "cpu",
            ["flavor_name"],
            ["project-1"],
        )


def test_generate_billing_csv_prices_cpu_buckets_as_instance_hours(monkeypatch) -> None:
    price = SimpleNamespace(
        resource_type="instance",
        metadata_field=None,
        metadata_value=None,
        unit_price=Decimal("2.00"),
        unit="hour",
    )
    project = SimpleNamespace(
        id="project-1",
        name="Example project",
        tags=["contract:CO-001"],
    )
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: [project]),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)
    request = {}

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: [price])
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {"CO-001": 1})
    monkeypatch.setattr(
        billing_runner,
        "_load_contract_customers",
        lambda db: {"CO-001": "Acme"},
    )
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        billing_runner,
        "_emit_synthetic_cluster_lines",
        lambda *args, **kwargs: None,
    )

    def query_usage(conn, begin, end, resource_type, metric_name, groupby_fields, project_ids):
        request.update(
            resource_type=resource_type,
            metric_name=metric_name,
            groupby_fields=groupby_fields,
            project_ids=project_ids,
        )
        return [
            {
                "project_id": "project-1",
                "metric": "cpu",
                "metadata": {"flavor_name": "b2.c1r2"},
                "hours": Decimal(2),
                "size_months": Decimal(0),
            }
        ]

    monkeypatch.setattr(billing_runner, "_query_gnocchi_usage", query_usage)

    report = generate_billing_csv(
        "postgresql://unused",
        "openstack",
        ["CO-001"],
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
    )

    assert request == {
        "resource_type": "instance",
        "metric_name": "cpu",
        "groupby_fields": ["flavor_name"],
        "project_ids": ["project-1"],
    }
    assert report == (
        "# Customer;ContractNumber;Project;ResourceType;Volume;Cost\r\n"
        "Acme;CO-001;Example project;instance (b2.c1r2);2,00 hour;4\r\n"
    )


def test_generate_billing_csv_rolls_up_canonical_volume_type_before_pricing(monkeypatch) -> None:
    price = SimpleNamespace(
        resource_type="volume.size",
        metadata_field="volume_type",
        metadata_value="rbd1",
        unit_price=Decimal("1.73"),
        unit="GB-month",
    )
    project = SimpleNamespace(
        id="project-1",
        name="Example project",
        tags=["contract:CO-001"],
    )
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: [project]),
        block_storage=SimpleNamespace(
            types=lambda: [SimpleNamespace(id="type-uuid", name="rbd1")]
        ),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: [price])
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {"CO-001": 1})
    monkeypatch.setattr(
        billing_runner,
        "_load_contract_customers",
        lambda db: {"CO-001": "Acme"},
    )
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        billing_runner,
        "_emit_synthetic_cluster_lines",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        billing_runner,
        "_query_gnocchi_usage",
        lambda *args: [
            {
                "project_id": "project-1",
                "metric": "volume.size",
                "metadata": {"volume_type": "type-uuid"},
                "hours": Decimal(1),
                "size_months": Decimal("4.648569"),
            },
            {
                "project_id": "project-1",
                "metric": "volume.size",
                "metadata": {"volume_type": "rbd1"},
                "hours": Decimal(1),
                "size_months": Decimal("40.311108"),
            },
        ],
    )

    report = generate_billing_csv(
        "postgresql://unused",
        "openstack",
        ["CO-001"],
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
    )

    assert report == (
        "# Customer;ContractNumber;Project;ResourceType;Volume;Cost\r\n"
        "Acme;CO-001;Example project;volume.size (rbd1);44,96 GB-month;78\r\n"
    )


def test_generate_billing_csv_prices_snapshot_and_backup_as_logical_gb_months(
    monkeypatch,
) -> None:
    prices = [
        SimpleNamespace(
            resource_type=metric,
            metadata_field=None,
            metadata_value=None,
            unit_price=Decimal("1.73"),
            unit="GB-month",
        )
        for metric in ("volume.snapshot.size", "volume.backup.size")
    ]
    project = SimpleNamespace(
        id="project-1",
        name="Example project",
        tags=["contract:CO-001"],
    )
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: [project]),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: prices)
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {"CO-001": 1})
    monkeypatch.setattr(
        billing_runner,
        "_load_contract_customers",
        lambda db: {"CO-001": "Acme"},
    )
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        billing_runner,
        "_emit_synthetic_cluster_lines",
        lambda *args, **kwargs: None,
    )

    def query_usage(conn, begin, end, resource_type, metric_name, groupby_fields, project_ids):
        quantity = {
            "volume.snapshot.size": Decimal(10),
            "volume.backup.size": Decimal(20),
        }[metric_name]
        return [
            {
                "project_id": "project-1",
                "metric": metric_name,
                "metadata": {},
                "hours": Decimal(700),
                "size_months": quantity,
            }
        ]

    monkeypatch.setattr(billing_runner, "_query_gnocchi_usage", query_usage)

    report = generate_billing_csv(
        "postgresql://unused",
        "openstack",
        ["CO-001"],
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
    )

    assert report == (
        "# Customer;ContractNumber;Project;ResourceType;Volume;Cost\r\n"
        "Acme;CO-001;Example project;volume.snapshot.size;10,00 GB-month;17\r\n"
        "Acme;CO-001;Example project;volume.backup.size;20,00 GB-month;35\r\n"
    )


def test_generate_billing_csv_fails_for_unpriced_flavor(monkeypatch) -> None:
    price = SimpleNamespace(
        resource_type="instance",
        metadata_field="flavor_name",
        metadata_value="b2.c1r2",
        unit_price=Decimal("2.00"),
        unit="hour",
    )
    project = SimpleNamespace(
        id="project-1",
        name="Example project",
        tags=["contract:CO-001"],
    )
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: [project]),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: [price])
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {"CO-001": 1})
    monkeypatch.setattr(
        billing_runner,
        "_load_contract_customers",
        lambda db: {"CO-001": "Acme"},
    )
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        billing_runner,
        "_query_gnocchi_usage",
        lambda *args: [
            {
                "project_id": "project-1",
                "metric": "cpu",
                "metadata": {"flavor_name": "b2.c2r4"},
                "hours": Decimal(1),
                "size_months": Decimal(0),
            }
        ],
    )

    with pytest.raises(BillingGenerationError, match="No price.*b2.c2r4"):
        generate_billing_csv(
            "postgresql://unused",
            "openstack",
            ["CO-001"],
            datetime(2026, 7, 1),
            datetime(2026, 8, 1),
        )


def test_generate_billing_csv_does_not_return_header_without_data(monkeypatch) -> None:
    connection = SimpleNamespace(
        identity=SimpleNamespace(projects=lambda: []),
    )
    database = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(billing_runner, "create_engine", lambda *args: engine)
    monkeypatch.setattr(billing_runner, "sessionmaker", lambda **kwargs: lambda: database)
    monkeypatch.setattr(billing_runner, "_load_prices", lambda db: [])
    monkeypatch.setattr(billing_runner, "_load_contract_overrides", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_rebates", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_ids", lambda db: {})
    monkeypatch.setattr(billing_runner, "_load_contract_customers", lambda db: {})
    monkeypatch.setattr(billing_runner.openstack, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        billing_runner,
        "_emit_synthetic_cluster_lines",
        lambda *args, **kwargs: None,
    )

    report = generate_billing_csv(
        "postgresql://unused",
        "openstack",
        [],
        datetime(2026, 7, 1),
        datetime(2026, 8, 1),
    )

    assert report == ""


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
