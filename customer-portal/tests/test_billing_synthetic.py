"""DB-backed tests for the synthetic cluster-billing emitter.

We seed Customer/Contract/TenantCluster/ClusterAddon/ClusterRequest via the
sync session, then call `_emit_synthetic_cluster_lines` and capture the rows
written to a fake CSV writer. The expected lines are derived from the plan's
billing model:

  - cluster_management_fee  500 SEK × (3 + 3·worker_groups) per period
                            (full month, never prorated)
  - cluster_setup_fee:controllers  1000 SEK × 1 in the period the cluster
                                   was provisioned
  - cluster_setup_fee:workers      2000 SEK × initial_worker_groups in the
                                   period the cluster was provisioned, AND
                                   2000 × Δgroups in any period an applied
                                   resize request lives
  - cluster_addon_fee:jupyterhub   3450 SEK × 1 per period the addon is
                                   active for any portion of the period
"""

from __future__ import annotations

import io
import json
from datetime import datetime

from app.billing_runner import _emit_synthetic_cluster_lines, _load_prices
from app.models import (
    ClusterAddon,
    ClusterRequest,
    Contract,
    ContractPriceOverride,
    Customer,
    TenantCluster,
)


def _writer():
    import csv

    buf = io.StringIO()
    return buf, csv.writer(buf, delimiter=";")


def _seed_customer_contract(s) -> tuple[Customer, Contract]:
    customer = Customer(name="Acme", domain="acme")
    s.add(customer)
    s.flush()
    contract = Contract(
        customer_id=customer.id,
        contract_number="CO-001",
        description="test",
    )
    s.add(contract)
    s.flush()
    return customer, contract


def _emit(s, period_start: datetime, period_end: datetime, contracts: set[str]):
    buf, writer = _writer()
    prices = _load_prices(s)
    contract_id_map = {c.contract_number: c.id for c in s.query(Contract).all()}
    _emit_synthetic_cluster_lines(
        s, period_start, period_end, contracts, prices, {}, {}, contract_id_map, writer
    )
    return buf.getvalue().splitlines()


def _emit_with_overrides(s, period_start, period_end, contracts, overrides, rebates):
    buf, writer = _writer()
    prices = _load_prices(s)
    contract_id_map = {c.contract_number: c.id for c in s.query(Contract).all()}
    _emit_synthetic_cluster_lines(
        s, period_start, period_end, contracts, prices, overrides, rebates, contract_id_map, writer
    )
    return buf.getvalue().splitlines()


def test_provisioning_period_emits_management_setup_addon(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Acme prod",
        slug="acme-prod",
        api_url="https://x",
        ca_bundle="dummy",
        openbao_mount="kubernetes/tenant-acme-prod",
        worker_groups=2,
        initial_worker_groups=2,  # Mellan: 6 workers + 3 controllers = 9 servers
        provisioned_at=datetime(2026, 4, 15),
        created_by_sub="admin@test",
    )
    s.add(cluster)
    s.flush()
    s.add(
        ClusterAddon(
            cluster_id=cluster.id,
            addon_type="jupyterhub",
            enabled_by_sub="admin@test",
            enabled_at=datetime(2026, 4, 20),
        )
    )
    s.commit()

    lines = _emit(s, datetime(2026, 4, 1), datetime(2026, 5, 1), {"CO-001"})

    # We expect:
    #   management fee: 9 vm-month × 500 = 4500
    #   controller setup: 1 × 1000
    #   worker setup (initial 2 groups): 2 × 2000 = 4000
    #   addon jupyterhub: 1 × 3450
    by_label = {line.split(";")[2]: line for line in lines}
    assert "Cluster management fee" in by_label
    assert "Controller setup fee" in by_label
    assert any("Worker setup fee (initial" in line for line in lines)
    assert "Addon: jupyterhub" in by_label

    mgmt = by_label["Cluster management fee"].split(";")
    assert mgmt[0] == "CO-001"
    assert mgmt[3] == "9 vm-month"
    assert mgmt[4] == "4500"

    ctrl = by_label["Controller setup fee"].split(";")
    assert ctrl[3] == "1 cluster"
    assert ctrl[4] == "1000"

    wkr = next(line for line in lines if "Worker setup fee (initial" in line).split(";")
    assert wkr[3] == "2 worker-group"
    assert wkr[4] == "4000"

    addon = by_label["Addon: jupyterhub"].split(";")
    assert addon[3] == "1 month"
    assert addon[4] == "3450"


def test_subsequent_period_emits_management_and_addon_only(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Acme prod",
        slug="acme-prod",
        api_url="https://x",
        ca_bundle="dummy",
        openbao_mount="kubernetes/tenant-acme-prod",
        worker_groups=1,
        initial_worker_groups=1,  # Liten: 3 + 3 = 6
        provisioned_at=datetime(2026, 3, 10),
        created_by_sub="admin@test",
    )
    s.add(cluster)
    s.flush()
    s.add(
        ClusterAddon(
            cluster_id=cluster.id,
            addon_type="jupyterhub",
            enabled_by_sub="admin@test",
            enabled_at=datetime(2026, 3, 11),
        )
    )
    s.commit()

    # April — provisioning was in March, so no setup line in April.
    lines = _emit(s, datetime(2026, 4, 1), datetime(2026, 5, 1), {"CO-001"})

    labels = [line.split(";")[2] for line in lines]
    assert "Cluster management fee" in labels
    assert "Addon: jupyterhub" in labels
    assert "Controller setup fee" not in labels
    assert not any("Worker setup fee" in la for la in labels)

    mgmt = next(line.split(";") for line in lines if line.split(";")[2] == "Cluster management fee")
    assert mgmt[3] == "6 vm-month"
    assert mgmt[4] == "3000"  # 6 × 500


def test_resize_period_emits_expansion_fee(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Acme prod",
        slug="acme-prod",
        api_url="https://x",
        ca_bundle="dummy",
        openbao_mount="kubernetes/tenant-acme-prod",
        worker_groups=3,            # current
        initial_worker_groups=1,
        provisioned_at=datetime(2026, 1, 5),
        created_by_sub="admin@test",
    )
    s.add(cluster)
    s.flush()
    # Two applied resize events; one in this period, one earlier (must not bill).
    s.add(
        ClusterRequest(
            cluster_id=cluster.id,
            request_type="resize",
            payload=json.dumps({"target_worker_groups": 2, "before_worker_groups": 1}),
            status="applied",
            requested_by_sub="admin@test",
            applied_by_sub="admin@test",
            applied_at=datetime(2026, 2, 12),  # earlier period, NOT billed in April
        )
    )
    s.add(
        ClusterRequest(
            cluster_id=cluster.id,
            request_type="resize",
            payload=json.dumps({"target_worker_groups": 3, "before_worker_groups": 2}),
            status="applied",
            requested_by_sub="admin@test",
            applied_by_sub="admin@test",
            applied_at=datetime(2026, 4, 14),  # this period
        )
    )
    s.commit()

    lines = _emit(s, datetime(2026, 4, 1), datetime(2026, 5, 1), {"CO-001"})
    expansion_lines = [line for line in lines if "Worker setup fee (expansion" in line]
    assert len(expansion_lines) == 1
    cols = expansion_lines[0].split(";")
    assert cols[2] == "Worker setup fee (expansion, +1 groups)"
    assert cols[3] == "1 worker-group"
    assert cols[4] == "2000"


def test_per_contract_override_applies(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Acme prod",
        slug="acme-prod",
        api_url="https://x",
        ca_bundle="dummy",
        openbao_mount="kubernetes/tenant-acme-prod",
        worker_groups=1,
        initial_worker_groups=1,
        provisioned_at=datetime(2026, 4, 5),
        created_by_sub="admin@test",
    )
    s.add(cluster)
    s.flush()
    s.add(
        ContractPriceOverride(
            contract_id=contract.id,
            resource_type="cluster_management_fee",
            unit_price=400,
        )
    )
    s.commit()

    overrides = {contract.id: {"cluster_management_fee": 400}}
    lines = _emit_with_overrides(
        s, datetime(2026, 4, 1), datetime(2026, 5, 1),
        {"CO-001"}, overrides, rebates={},
    )
    mgmt = next(line.split(";") for line in lines if line.split(";")[2] == "Cluster management fee")
    # 6 VMs × 400 (override) = 2400
    assert mgmt[4] == "2400"


def test_disabled_addon_not_billed_after_disable(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    cluster = TenantCluster(
        contract_id=contract.id,
        name="Acme prod",
        slug="acme-prod",
        api_url="https://x",
        ca_bundle="dummy",
        openbao_mount="kubernetes/tenant-acme-prod",
        worker_groups=1,
        initial_worker_groups=1,
        provisioned_at=datetime(2026, 1, 1),
        created_by_sub="admin@test",
    )
    s.add(cluster)
    s.flush()
    s.add(
        ClusterAddon(
            cluster_id=cluster.id,
            addon_type="jupyterhub",
            enabled_by_sub="admin@test",
            enabled_at=datetime(2026, 1, 5),
            disabled_at=datetime(2026, 3, 31),
            disabled_by_sub="admin@test",
        )
    )
    s.commit()

    # April — addon was disabled on March 31 (before April 1), so no bill.
    lines = _emit(s, datetime(2026, 4, 1), datetime(2026, 5, 1), {"CO-001"})
    labels = [line.split(";")[2] for line in lines]
    assert "Addon: jupyterhub" not in labels


def test_unprovisioned_cluster_not_billed(sync_session):
    s = sync_session
    _, contract = _seed_customer_contract(s)
    s.add(
        TenantCluster(
            contract_id=contract.id,
            name="Acme prod",
            slug="acme-prod",
            api_url="https://x",
            ca_bundle="dummy",
            openbao_mount="kubernetes/tenant-acme-prod",
            worker_groups=2,
            initial_worker_groups=2,
            provisioned_at=None,  # not yet live
            created_by_sub="admin@test",
        )
    )
    s.commit()

    lines = _emit(s, datetime(2026, 4, 1), datetime(2026, 5, 1), {"CO-001"})
    assert lines == []
