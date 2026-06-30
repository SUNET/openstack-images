"""Admin endpoints for managing customers, contracts, access, and pricing."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit_log
from app.auth import require_admin
from app.db import get_session
from app.models import (
    Contract,
    ContractAccess,
    ContractPriceOverride,
    ContractRebate,
    Customer,
    ResourcePrice,
)
from app.schemas import (
    ContractDetailResponse,
    ContractPriceOverrideRequest,
    ContractPriceOverrideResponse,
    ContractRebateRequest,
    ContractResponse,
    CreateContractRequest,
    CreateCustomerRequest,
    CustomerDetailResponse,
    CustomerResponse,
    GrantAccessRequest,
    MoveContractRequest,
    MoveProjectRequest,
    ProjectResponse,
    RenameContractRequest,
    ResourcePriceRequest,
    ResourcePriceResponse,
    UpdateContractRequest,
    UpdateCustomerRequest,
)
from app.routers.projects import _enrich_project

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Customers ---


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Customer).order_by(Customer.name))
    return result.scalars().all()


@router.post("/customers", response_model=CustomerResponse, status_code=201)
async def create_customer(
    req: CreateCustomerRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    existing = await session.execute(select(Customer).where(Customer.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Customer already exists")

    customer = Customer(name=req.name, domain=req.domain, description=req.description)
    session.add(customer)
    await session.commit()
    await session.refresh(customer)
    audit_log(user["sub"], "customer.create", customer_id=customer.id, name=customer.name)
    return customer


@router.get("/customers/{customer_id}", response_model=CustomerDetailResponse)
async def get_customer(
    customer_id: int,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Customer)
        .where(Customer.id == customer_id)
        .options(selectinload(Customer.contracts))
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.patch("/customers/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: int,
    req: UpdateCustomerRequest,
    request: Request,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    customer = await session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if req.domain is not None and req.domain != customer.domain:
        # Check if any projects exist under this customer's contracts
        git_backend = request.app.state.git_backend
        contracts = await session.execute(
            select(Contract).where(Contract.customer_id == customer_id)
        )
        for contract in contracts.scalars():
            projects = git_backend.list_projects(contract.contract_number)
            if projects:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot change domain while projects exist",
                )
        customer.domain = req.domain

    if req.name is not None:
        if req.name != customer.name:
            existing = await session.execute(
                select(Customer).where(Customer.name == req.name)
            )
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Customer name already exists")
            customer.name = req.name

    if req.description is not None:
        customer.description = req.description

    await session.commit()
    await session.refresh(customer)
    audit_log(user["sub"], "customer.update", customer_id=customer.id)
    return customer


@router.delete("/customers/{customer_id}", status_code=204)
async def delete_customer(
    customer_id: int,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    customer = await session.execute(
        select(Customer)
        .where(Customer.id == customer_id)
        .options(selectinload(Customer.contracts))
    )
    customer = customer.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if customer.contracts:
        raise HTTPException(status_code=409, detail="Cannot delete customer with contracts")

    await session.delete(customer)
    await session.commit()
    audit_log(user["sub"], "customer.delete", customer_id=customer_id)


# --- Contracts ---


@router.get("/contracts", response_model=list[ContractResponse])
async def list_contracts(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Contract).order_by(Contract.contract_number))
    return result.scalars().all()


@router.post("/contracts", response_model=ContractResponse, status_code=201)
async def create_contract(
    req: CreateContractRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    customer = await session.get(Customer, req.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    existing = await session.execute(
        select(Contract).where(Contract.contract_number == req.contract_number)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Contract number already exists")

    contract = Contract(
        customer_id=req.customer_id,
        contract_number=req.contract_number,
        description=req.description,
    )
    session.add(contract)
    await session.commit()
    await session.refresh(contract)
    audit_log(
        user["sub"], "contract.create",
        contract_id=contract.id, contract_number=contract.contract_number,
    )
    return contract


@router.get("/contracts/{contract_id}", response_model=ContractDetailResponse)
async def get_contract(
    contract_id: int,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Contract)
        .where(Contract.id == contract_id)
        .options(
            selectinload(Contract.customer),
            selectinload(Contract.access_grants),
            selectinload(Contract.rebate),
        )
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    return ContractDetailResponse(
        id=contract.id,
        customer_id=contract.customer_id,
        contract_number=contract.contract_number,
        description=contract.description,
        created_at=contract.created_at,
        updated_at=contract.updated_at,
        customer=contract.customer,
        users=[g.user_sub for g in contract.access_grants],
        rebate_percent=contract.rebate.rebate_percent if contract.rebate else None,
    )


@router.patch("/contracts/{contract_id}", response_model=ContractResponse)
async def update_contract(
    contract_id: int,
    req: UpdateContractRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    if req.description is not None:
        contract.description = req.description

    await session.commit()
    await session.refresh(contract)
    audit_log(user["sub"], "contract.update", contract_id=contract.id)
    return contract


@router.post("/contracts/{contract_id}/move", response_model=ContractResponse)
async def move_contract(
    contract_id: int,
    req: MoveContractRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Reassign a contract to a different customer.

    Existing projects keep their current names (which embed the old customer's
    domain) — only the customer link changes. Renaming projects would be
    destructive, so it is intentionally out of scope.
    """
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    customer = await session.get(Customer, req.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Target customer not found")

    if contract.customer_id == req.customer_id:
        raise HTTPException(status_code=409, detail="Contract already belongs to this customer")

    old_customer_id = contract.customer_id
    contract.customer_id = req.customer_id
    await session.commit()
    await session.refresh(contract)
    audit_log(
        user["sub"], "contract.move",
        contract_id=contract.id, contract_number=contract.contract_number,
        from_customer_id=old_customer_id, to_customer_id=req.customer_id,
    )
    return contract


@router.post("/contracts/{contract_id}/rename", response_model=ContractResponse)
async def rename_contract(
    contract_id: int,
    req: RenameContractRequest,
    request: Request,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Change a contract's number, re-pointing all its projects.

    The new number must be unused. Every project linked to the old number has
    its `spec.contractNumber` rewritten in git; CR names (OpenStack identity)
    are untouched, so the rename is non-destructive. The DB change is committed
    only after the git rewrite succeeds.
    """
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    old_number = contract.contract_number
    new_number = req.contract_number
    if new_number == old_number:
        raise HTTPException(status_code=409, detail="Contract already has this number")

    result = await session.execute(
        select(Contract).where(Contract.contract_number == new_number)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A contract with this number already exists")

    contract.contract_number = new_number
    await session.flush()

    git_backend = request.app.state.git_backend
    try:
        count = git_backend.rename_contract(old_number, new_number)
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to re-point projects: {e}")

    await session.commit()
    await session.refresh(contract)
    audit_log(
        user["sub"], "contract.rename",
        contract_id=contract.id, from_number=old_number,
        to_number=new_number, projects_repointed=count,
    )
    return contract


@router.delete("/contracts/{contract_id}", status_code=204)
async def delete_contract(
    contract_id: int,
    request: Request,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    # Check if contract has projects in git
    git_backend = request.app.state.git_backend
    projects = git_backend.list_projects(contract.contract_number)
    if projects:
        raise HTTPException(status_code=409, detail="Cannot delete contract with projects")

    await session.delete(contract)
    await session.commit()
    audit_log(user["sub"], "contract.delete", contract_id=contract_id)


# --- Contract Access ---


@router.post("/contracts/{contract_id}/users", status_code=201)
async def grant_access(
    contract_id: int,
    req: GrantAccessRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    existing = await session.execute(
        select(ContractAccess).where(
            ContractAccess.contract_id == contract_id,
            ContractAccess.user_sub == req.user_sub,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="User already has access")

    grant = ContractAccess(contract_id=contract_id, user_sub=req.user_sub)
    session.add(grant)
    await session.commit()
    audit_log(
        user["sub"], "contract.grant_access",
        contract_id=contract_id, target_sub=req.user_sub,
    )
    return {"status": "granted", "user_sub": req.user_sub}


@router.delete("/contracts/{contract_id}/users/{user_sub}", status_code=204)
async def revoke_access(
    contract_id: int,
    user_sub: str,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ContractAccess).where(
            ContractAccess.contract_id == contract_id,
            ContractAccess.user_sub == user_sub,
        )
    )
    grant = result.scalar_one_or_none()
    if not grant:
        raise HTTPException(status_code=404, detail="Access grant not found")

    await session.delete(grant)
    await session.commit()
    audit_log(
        user["sub"], "contract.revoke_access",
        contract_id=contract_id, target_sub=user_sub,
    )


# --- Global Pricing ---


@router.get("/pricing/metrics")
async def list_available_metrics(
    _user=Depends(require_admin),
):
    """Discover available metric types and metadata values from Gnocchi."""
    from app.billing_runner import discover_gnocchi_metrics
    from app.config import get_settings

    settings = get_settings()
    metrics = await asyncio.to_thread(discover_gnocchi_metrics, settings.openstack_cloud)
    return metrics


@router.get("/pricing", response_model=list[ResourcePriceResponse])
async def list_prices(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ResourcePrice).order_by(ResourcePrice.resource_type)
    )
    return result.scalars().all()


@router.post("/pricing", response_model=ResourcePriceResponse, status_code=201)
async def create_price(
    req: ResourcePriceRequest,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Create a price for a resource type, optionally scoped to a metadata value."""
    # Check for duplicate
    query = select(ResourcePrice).where(
        ResourcePrice.resource_type == req.resource_type,
    )
    if req.metadata_field:
        query = query.where(
            ResourcePrice.metadata_field == req.metadata_field,
            ResourcePrice.metadata_value == req.metadata_value,
        )
    else:
        query = query.where(ResourcePrice.metadata_field.is_(None))

    existing = await session.execute(query)
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Price already exists for this combination")

    price = ResourcePrice(
        resource_type=req.resource_type,
        unit_price=req.unit_price,
        unit=req.unit,
        metadata_field=req.metadata_field,
        metadata_value=req.metadata_value,
    )
    session.add(price)
    await session.commit()
    await session.refresh(price)
    return price


@router.delete("/pricing/{price_id}", status_code=204)
async def delete_price(
    price_id: int,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    price = await session.get(ResourcePrice, price_id)
    if not price:
        raise HTTPException(status_code=404, detail="Price not found")

    await session.delete(price)
    await session.commit()


# --- Contract Price Overrides ---


@router.get(
    "/contracts/{contract_id}/pricing",
    response_model=list[ContractPriceOverrideResponse],
)
async def list_contract_prices(
    contract_id: int,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ContractPriceOverride)
        .where(ContractPriceOverride.contract_id == contract_id)
        .order_by(ContractPriceOverride.resource_type)
    )
    return result.scalars().all()


@router.put(
    "/contracts/{contract_id}/pricing/{resource_type}",
    response_model=ContractPriceOverrideResponse,
)
async def set_contract_price(
    contract_id: int,
    resource_type: str,
    req: ContractPriceOverrideRequest,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    result = await session.execute(
        select(ContractPriceOverride).where(
            ContractPriceOverride.contract_id == contract_id,
            ContractPriceOverride.resource_type == resource_type,
        )
    )
    override = result.scalar_one_or_none()

    if override:
        override.unit_price = req.unit_price
    else:
        override = ContractPriceOverride(
            contract_id=contract_id,
            resource_type=req.resource_type,
            unit_price=req.unit_price,
        )
        session.add(override)

    await session.commit()
    await session.refresh(override)
    return override


@router.delete("/contracts/{contract_id}/pricing/{resource_type}", status_code=204)
async def delete_contract_price(
    contract_id: int,
    resource_type: str,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ContractPriceOverride).where(
            ContractPriceOverride.contract_id == contract_id,
            ContractPriceOverride.resource_type == resource_type,
        )
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="Price override not found")

    await session.delete(override)
    await session.commit()


# --- Contract Rebates ---


@router.put("/contracts/{contract_id}/rebate")
async def set_rebate(
    contract_id: int,
    req: ContractRebateRequest,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    contract = await session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    result = await session.execute(
        select(ContractRebate).where(ContractRebate.contract_id == contract_id)
    )
    rebate = result.scalar_one_or_none()

    if rebate:
        rebate.rebate_percent = req.rebate_percent
    else:
        rebate = ContractRebate(
            contract_id=contract_id,
            rebate_percent=req.rebate_percent,
        )
        session.add(rebate)

    await session.commit()
    return {"rebate_percent": float(req.rebate_percent)}


@router.delete("/contracts/{contract_id}/rebate", status_code=204)
async def delete_rebate(
    contract_id: int,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ContractRebate).where(ContractRebate.contract_id == contract_id)
    )
    rebate = result.scalar_one_or_none()
    if not rebate:
        raise HTTPException(status_code=404, detail="Rebate not found")

    await session.delete(rebate)
    await session.commit()


# --- Projects (admin moves) ---


@router.post("/projects/{resource_name}/move", response_model=ProjectResponse)
async def move_project(
    resource_name: str,
    req: MoveProjectRequest,
    request: Request,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Reassign a project to a different contract.

    Only `spec.contractNumber` in the CR changes; the project keeps its name
    and OpenStack identity, so no resources are touched. Billing follows the
    new contract from the next run onward.
    """
    result = await session.execute(
        select(Contract).where(Contract.contract_number == req.contract_number)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Target contract not found")

    git_backend = request.app.state.git_backend
    existing = git_backend.get_project(resource_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")

    if existing["contract_number"] == req.contract_number:
        raise HTTPException(status_code=409, detail="Project already belongs to this contract")

    try:
        updated = git_backend.move_project(resource_name, req.contract_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    audit_log(
        user["sub"], "project.move",
        resource_name=resource_name,
        from_contract=existing["contract_number"], to_contract=req.contract_number,
    )
    return _enrich_project(updated)
