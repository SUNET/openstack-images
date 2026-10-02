"""Tests for bounded billing report shards and checkpoint aggregation."""

import threading
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app import billing_reports, billing_runner
from app.billing_reports import _merge_usage, _windows
from app.billing_runner import (
    _query_gnocchi_usage,
    render_billing_csv,
    serialize_billing_input_snapshot,
)
from tests.test_migration_015 import GitOpsDatabase
from tests.test_migration_015 import gitops_database as gitops_database
from tests.test_migration_015 import gitops_postgres as gitops_postgres


def _snapshot() -> dict:
    return {
        "version": 1,
        "artifact": {"filename_template": "billing.csv", "per_contract": False},
        "period": {
            "start": "2026-09-01T00:00:00+00:00",
            "end": "2026-10-01T00:00:00+00:00",
        },
        "filename_variables": {
            "year": "2026",
            "month": "09",
            "day": "01",
            "date": "2026-10-01",
        },
        "contracts": [
            {"id": 1, "number": "CO-001", "customer_name": "Frozen customer"}
        ],
        "projects": [
            {
                "id": "frozen-project",
                "name": "Frozen project",
                "contract_number": "CO-001",
            }
        ],
        "prices": [
            {
                "resource_type": "volume.size",
                "metadata_field": "volume_type",
                "metadata_value": "frozen-fast",
                "unit_price": "2.00",
                "unit": "GB-month",
            },
            {
                "resource_type": "cluster_management_fee",
                "metadata_field": "worker_groups",
                "metadata_value": "1",
                "unit_price": "100.00",
                "unit": "cluster-month",
            },
            {
                "resource_type": "cluster_setup_fee",
                "metadata_field": "group_type",
                "metadata_value": "controllers",
                "unit_price": "50.00",
                "unit": "cluster",
            },
            {
                "resource_type": "cluster_setup_fee",
                "metadata_field": "group_type",
                "metadata_value": "workers",
                "unit_price": "20.00",
                "unit": "worker-group",
            },
            {
                "resource_type": "cluster_addon_fee",
                "metadata_field": "addon",
                "metadata_value": "notebook",
                "unit_price": "30.00",
                "unit": "month",
            },
        ],
        "overrides": [
            {
                "contract_number": "CO-001",
                "resource_type": "volume.size",
                "unit_price": "3.00",
            }
        ],
        "rebates": [
            {"contract_number": "CO-001", "rebate_percent": "10.00"}
        ],
        "cinder_volume_types": {"frozen-type-id": "frozen-fast"},
        "query_plan": [
            {
                "metric": "volume.size",
                "resource_type": "frozen-volume",
                "source_metric": "frozen.metric",
                "metadata_fields": ["volume_type"],
                "aggregation": "additive_size",
                "unit": "GB-month",
                "size_gb_scale": "1",
            }
        ],
        "synthetic": {
            "clusters": [
                {
                    "contract_number": "CO-001",
                    "slug": "frozen-cluster",
                    "worker_groups": 1,
                    "initial_worker_groups": 2,
                    "provisioned_at": "2026-09-02T00:00:00+00:00",
                }
            ],
            "resizes": [
                {
                    "applied_at": "2026-09-15T00:00:00+00:00",
                    "contract_number": "CO-001",
                    "delta_worker_groups": 1,
                    "slug": "frozen-cluster",
                }
            ],
            "addons": [
                {
                    "addon_type": "notebook",
                    "contract_number": "CO-001",
                    "disabled_at": None,
                    "enabled_at": "2026-09-10T00:00:00+00:00",
                    "slug": "frozen-cluster",
                }
            ],
        },
    }


def test_initial_windows_cover_period_without_overlap() -> None:
    start = datetime(2026, 9, 1)
    end = datetime(2026, 10, 1)

    windows = list(_windows(start, end))

    assert windows[0][0] == start
    assert windows[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(windows, windows[1:]))
    assert all((stop - begin).days <= 7 for begin, stop in windows)


def test_snapshot_serialization_is_canonical_and_type_safe() -> None:
    left = {
        "version": 1,
        "decimal": Decimal("1.20"),
        "timestamp": datetime(2026, 9, 1),
    }
    right = dict(reversed(list(left.items())))

    assert serialize_billing_input_snapshot(left) == (
        serialize_billing_input_snapshot(right)
    )
    assert serialize_billing_input_snapshot(left) == (
        '{"decimal":"1.20","timestamp":"2026-09-01T00:00:00+00:00",'
        '"version":1}'
    )


def test_shard_uses_full_report_for_size_normalization(monkeypatch) -> None:
    group = {
        "group": {"project_id": "project-1", "volume_type": "fast"},
        "measures": {
            "measures": {
                "aggregated": [["2026-07-01T00:00:00+00:00", 3600, 20]]
            }
        },
    }
    response = SimpleNamespace(
        status_code=200,
        content=b"response",
        json=lambda: [group],
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    usage = _query_gnocchi_usage(
        SimpleNamespace(auth_token="token"),
        datetime(2026, 7, 1),
        datetime(2026, 7, 1, 1),
        "volume",
        "volume.size",
        ["volume_type"],
        ["project-1"],
        aggregate_across_resources=True,
        normalization_period_seconds=Decimal(7200),
    )

    assert usage[0]["size_months"] == Decimal(10)


def test_checkpoint_merge_sums_matching_pricing_groups() -> None:
    usage = (
        '[{"hours":"0","metadata":{"volume_type":"fast"},'
        '"metric":"volume.size","project_id":"project-1",'
        '"size_months":"2.5"}]'
    )
    shards = [
        SimpleNamespace(metric="volume.size", usage_json=usage),
        SimpleNamespace(metric="volume.size", usage_json=usage),
    ]

    merged = _merge_usage(shards)

    assert merged["volume.size"][0]["size_months"] == Decimal("5.0")


def test_per_contract_rendering_produces_individual_csv_artifacts() -> None:
    snapshot = _snapshot()
    snapshot["artifact"] = {
        "filename_template": "billing-{year}-{month}.csv",
        "per_contract": True,
    }
    snapshot["contracts"].append(
        {"id": 2, "number": "CO-002", "customer_name": "Second customer"}
    )
    second_cluster = dict(snapshot["synthetic"]["clusters"][0])
    second_cluster.update(contract_number="CO-002", slug="second-cluster")
    snapshot["synthetic"]["clusters"].append(second_cluster)

    outputs = billing_reports._artifacts(snapshot, {})

    assert [output[0] for output in outputs] == [
        "billing-2026-09-CO-001.csv",
        "billing-2026-09-CO-002.csv",
    ]
    assert b"CO-001" in outputs[0][2] and b"CO-002" not in outputs[0][2]
    assert b"CO-002" in outputs[1][2] and b"CO-001" not in outputs[1][2]


def test_oversized_response_requests_shard_split(monkeypatch) -> None:
    response = SimpleNamespace(
        status_code=200,
        content=b"x" * 11,
        json=lambda: [],
    )
    monkeypatch.setattr(billing_runner, "MAX_GNOCCHI_RESPONSE_BYTES", 10)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    try:
        _query_gnocchi_usage(
            SimpleNamespace(auth_token="token"),
            datetime(2026, 7, 1),
            datetime(2026, 7, 8),
            "volume",
            "volume.size",
            ["volume_type"],
            ["project-1"],
            aggregate_across_resources=True,
        )
    except billing_runner.GnocchiShardTooLarge:
        pass
    else:
        raise AssertionError("oversized response did not request a split")


def test_snapshot_rendering_uses_frozen_inputs_without_live_readers(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("snapshot rendering invoked a live reader")

    monkeypatch.setattr(billing_runner, "create_engine", unexpected)
    monkeypatch.setattr(billing_runner.openstack, "connect", unexpected)
    monkeypatch.setattr(billing_runner, "_load_prices", unexpected)
    monkeypatch.setattr(billing_runner, "_get_cinder_volume_type_names", unexpected)
    usage = {
        "volume.size": [
            {
                "project_id": "frozen-project",
                "metric": "frozen.metric",
                "metadata": {"volume_type": "frozen-type-id"},
                "hours": Decimal(0),
                "size_months": Decimal(10),
            }
        ]
    }

    rendered = render_billing_csv(_snapshot(), usage)

    assert "Frozen customer;CO-001;Frozen project" in rendered
    assert "volume.size (frozen-fast);10.00;GB-month;27" in rendered
    assert "managed-cluster:frozen-cluster;Cluster management fee" in rendered
    assert "Controller setup fee;1;cluster;45" in rendered
    assert "Worker setup fee (initial, 2 groups);2;worker-group;36" in rendered
    assert "Worker setup fee (expansion, +1 groups);1;worker-group;18" in rendered
    assert rendered.endswith("Addon: notebook;1;month;27\r\n")


def test_process_report_plans_and_queries_only_from_persisted_snapshot(
    gitops_database: GitOpsDatabase,
    monkeypatch,
) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.billing_runner import serialize_billing_input_snapshot
    from app.models import BillingReport, BillingReportShard

    gitops_database.migrate("017")
    engine = create_engine(gitops_database.sync_url)
    snapshot = _snapshot()
    report_id = "snapshot-report"
    report = BillingReport(
        id=report_id,
        requested_by_sub="admin@test",
        status="running",
        billing_period_start=datetime(2026, 9, 1),
        billing_period_end=datetime(2026, 10, 1),
        contract_numbers_json='["CO-001"]',
        input_snapshot_json=serialize_billing_input_snapshot(snapshot),
        filename_template="billing.csv",
        per_contract=False,
        progress_current=0,
        progress_total=0,
    )
    with Session(engine) as session:
        session.add(report)
        session.commit()

    calls = []

    def query(conn, begin, end, resource_type, source_metric, fields, projects, **kwargs):
        calls.append((begin, resource_type, source_metric, fields, projects))
        if begin.day in {1, 15, 29}:
            return []
        return [
            {
                "project_id": "frozen-project",
                "metric": "frozen.metric",
                "metadata": {"volume_type": "frozen-type-id"},
                "hours": Decimal(0),
                "size_months": Decimal("1.2"),
            }
        ]

    monkeypatch.setattr(billing_reports, "_query_gnocchi_usage", query)
    monkeypatch.setattr(
        billing_reports,
        "capture_billing_input_snapshot",
        lambda *args, **kwargs: pytest.fail("persisted snapshot was recaptured"),
    )
    monkeypatch.setattr(
        billing_reports.openstack,
        "connect",
        lambda **kwargs: SimpleNamespace(auth_token="token"),
    )
    monkeypatch.setitem(
        billing_runner.GNOCCHI_PRODUCT_REGISTRY,
        "volume.size",
        {
            "resource_type": "mutated-volume",
            "source_metric": "mutated.metric",
            "metadata_fields": {"mutated"},
            "aggregation": "resource_hours",
            "unit": "mutated",
            "size_gb_scale": None,
        },
    )

    try:
        billing_reports.process_report(
            gitops_database.sync_url.render_as_string(hide_password=False),
            "test",
            report_id,
        )
        with Session(engine) as session:
            shards = session.scalars(
                select(BillingReportShard).order_by(BillingReportShard.id)
            ).all()
            stored = session.scalar(
                select(BillingReport).where(BillingReport.id == report_id)
            )
    finally:
        engine.dispose()

    assert {(shard.metric, shard.project_id) for shard in shards} == {
        ("volume.size", "frozen-project")
    }
    assert calls
    assert all(
        call[1:]
        == (
            "frozen-volume",
            "frozen.metric",
            ["volume_type"],
            ["frozen-project"],
        )
        for call in calls
    )
    assert stored is not None and stored.status == "succeeded"
    assert b"Frozen project;volume.size (frozen-fast);2.40;GB-month;6" in (
        stored.result_content or b""
    )
    assert b"Frozen customer;CO-001;managed-cluster:frozen-cluster" in (
        stored.result_content or b""
    )
    assert (stored.result_content or b"").endswith(b"Addon: notebook;1;month;27\r\n")


def test_delivery_resume_skips_sent_output_and_completes_linked_run(
    gitops_database: GitOpsDatabase,
    monkeypatch,
) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models import (
        BillingJob,
        BillingJobRun,
        BillingReport,
        BillingReportOutput,
    )

    gitops_database.migrate("017")
    engine = create_engine(gitops_database.sync_url)
    snapshot = _snapshot()
    snapshot["query_plan"] = []
    delivered_at = datetime(2026, 10, 1)
    with Session(engine) as session:
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
        session.flush()
        run = BillingJobRun(
            billing_job_id=job.id,
            billing_period_start=datetime(2026, 9, 1),
            billing_period_end=datetime(2026, 10, 1),
            status="running",
        )
        session.add(run)
        session.flush()
        report = BillingReport(
            id="delivery-resume",
            billing_job_run_id=run.id,
            requested_by_sub="admin@test",
            status="running",
            billing_period_start=datetime(2026, 9, 1),
            billing_period_end=datetime(2026, 10, 1),
            contract_numbers_json='["CO-001"]',
            input_snapshot_json=serialize_billing_input_snapshot(snapshot),
            filename_template="billing-{contract}.csv",
            per_contract=True,
            delivery_method="email",
            delivery_config='{"recipient":"billing@example.test"}',
            progress_current=0,
            progress_total=0,
        )
        report.outputs.extend(
            [
                BillingReportOutput(
                    filename="already-sent.csv",
                    media_type="text/csv; charset=utf-8",
                    content=b"sent",
                    sha256="a" * 64,
                    size=4,
                    status="sent",
                    delivered_at=delivered_at,
                ),
                BillingReportOutput(
                    filename="pending.csv",
                    media_type="text/csv; charset=utf-8",
                    content=b"pending",
                    sha256="b" * 64,
                    size=7,
                    status="pending",
                ),
            ]
        )
        session.add(report)
        session.commit()
        run_id = run.id

    delivered = []

    async def deliver(method, config, filename, content):
        delivered.append((method, config, filename, content))

    monkeypatch.setattr(billing_reports, "_deliver", deliver)
    monkeypatch.setattr(
        billing_reports,
        "_artifacts",
        lambda *args: pytest.fail("checkpointed outputs were regenerated"),
    )
    try:
        billing_reports.process_report(
            gitops_database.sync_url.render_as_string(hide_password=False),
            "test",
            "delivery-resume",
        )
        with Session(engine) as session:
            stored_report = session.get(BillingReport, "delivery-resume")
            stored_run = session.get(BillingJobRun, run_id)
            outputs = list(
                session.scalars(
                    select(BillingReportOutput).order_by(BillingReportOutput.id)
                )
            )
    finally:
        engine.dispose()

    assert delivered == [
        (
            "email",
            {"recipient": "billing@example.test"},
            "pending.csv",
            "pending",
        )
    ]
    assert [output.status for output in outputs] == ["sent", "sent"]
    assert outputs[0].delivered_at == delivered_at
    assert outputs[1].delivered_at is not None
    assert stored_report is not None and stored_report.status == "succeeded"
    assert stored_report.delivery_config is None
    assert stored_report.expires_at is not None
    assert stored_run is not None and stored_run.status == "success"
    assert stored_run.files_delivered == 2


def test_processor_lock_survives_loss_of_worker_lock(
    gitops_database: GitOpsDatabase,
    monkeypatch,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import BillingReport

    gitops_database.migrate("017")
    engine = create_engine(gitops_database.sync_url)
    snapshot = _snapshot()
    report = BillingReport(
        id="processor-lock",
        requested_by_sub="admin@test",
        status="running",
        billing_period_start=datetime(2026, 9, 1),
        billing_period_end=datetime(2026, 10, 1),
        contract_numbers_json='["CO-001"]',
        input_snapshot_json=serialize_billing_input_snapshot(snapshot),
        filename_template="billing.csv",
        per_contract=False,
        progress_current=0,
        progress_total=0,
    )
    with Session(engine) as session:
        session.add(report)
        session.commit()

    started = threading.Event()
    release = threading.Event()

    def query(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(billing_reports, "_query_gnocchi_usage", query)
    monkeypatch.setattr(
        billing_reports.openstack,
        "connect",
        lambda **kwargs: SimpleNamespace(auth_token="token"),
    )
    results = []
    errors = []

    def process() -> None:
        try:
            results.append(
                billing_reports.process_report(
                    gitops_database.sync_url.render_as_string(hide_password=False),
                    "test",
                    "processor-lock",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=process)
    first.start()
    try:
        assert started.wait(timeout=5), errors
        assert (
            billing_reports.process_report(
                gitops_database.sync_url.render_as_string(hide_password=False),
                "test",
                "processor-lock",
            )
            is False
        )
    finally:
        release.set()
        first.join(timeout=5)
        engine.dispose()

    assert not first.is_alive()
    assert errors == []
    assert results == [True]


def test_process_report_documents_smtp_at_least_once_semantics() -> None:
    docstring = billing_reports.process_report.__doc__ or ""
    assert "SMTP is" in docstring
    assert "at-least-once" in docstring
