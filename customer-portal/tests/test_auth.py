"""Tests for portal authorization helpers."""

from types import SimpleNamespace

import pytest

from app import auth
from app.models import Contract, ContractAccess, Customer


@pytest.mark.asyncio
async def test_sunet_admin_sees_all_contracts(session, monkeypatch) -> None:
    """A SUNET admin sees contracts without explicit access grants."""
    settings = SimpleNamespace(admin_users=["admin@test"])
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: settings,
    )
    customer = Customer(
        name="Example",
        domain="example.test",
    )
    session.add(customer)
    await session.flush()
    first = Contract(
        customer_id=customer.id,
        contract_number="CO-001",
    )
    second = Contract(
        customer_id=customer.id,
        contract_number="CO-002",
    )
    session.add_all([first, second])
    await session.flush()
    contracts = await auth.get_user_contracts(
        "admin@test",
        session,
    )
    assert {contract.contract_number for contract in contracts} == {"CO-001", "CO-002"}


@pytest.mark.asyncio
async def test_regular_user_sees_only_granted_contracts(session, monkeypatch) -> None:
    """A regular user sees only contracts explicitly granted to them."""
    settings = SimpleNamespace(admin_users=["admin@test"])
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: settings,
    )
    customer = Customer(
        name="Example",
        domain="example.test",
    )
    session.add(customer)
    await session.flush()
    first = Contract(
        customer_id=customer.id,
        contract_number="CO-001",
    )
    second = Contract(
        customer_id=customer.id,
        contract_number="CO-002",
    )
    session.add_all([first, second])
    await session.flush()
    session.add(
        ContractAccess(
            contract_id=first.id,
            user_sub="user@test",
        )
    )

    await session.flush()
    contracts = await auth.get_user_contracts(
        "user@test",
        session,
    )
    assert [contract.contract_number for contract in contracts] == ["CO-001"]
