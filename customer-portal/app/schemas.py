"""Pydantic request/response schemas."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

# --- Admin: Customers ---


class CreateCustomerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9.-]+$")
    description: str = ""


class UpdateCustomerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[a-z0-9.-]+$")
    description: str | None = None


class CustomerResponse(BaseModel):
    id: int
    name: str
    domain: str
    description: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CustomerDetailResponse(CustomerResponse):
    contracts: list["ContractResponse"] = []


# --- Admin: Contracts ---


class CreateContractRequest(BaseModel):
    customer_id: int
    contract_number: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
    description: str = ""


class UpdateContractRequest(BaseModel):
    description: str | None = None


class ContractResponse(BaseModel):
    id: int
    customer_id: int
    contract_number: str
    description: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ContractWithCustomerResponse(ContractResponse):
    customer: CustomerResponse


class ContractDetailResponse(ContractResponse):
    customer: CustomerResponse
    users: list[str] = []
    rebate_percent: Decimal | None = None


# --- Admin: Contract Access ---


class GrantAccessRequest(BaseModel):
    user_sub: str = Field(min_length=1, max_length=255)


# --- Admin: Pricing ---


class ResourcePriceRequest(BaseModel):
    resource_type: str = Field(min_length=1, max_length=100)
    unit_price: Decimal = Field(ge=0)
    unit: str = Field(min_length=1, max_length=50)
    metadata_field: str | None = Field(default=None, max_length=100)
    metadata_value: str | None = Field(default=None, max_length=255)


class ResourcePriceResponse(BaseModel):
    id: int
    resource_type: str
    unit_price: Decimal
    unit: str
    metadata_field: str | None = None
    metadata_value: str | None = None

    model_config = {"from_attributes": True}


class ContractPriceOverrideRequest(BaseModel):
    resource_type: str = Field(min_length=1, max_length=100)
    unit_price: Decimal = Field(ge=0)


class ContractPriceOverrideResponse(BaseModel):
    id: int
    contract_id: int
    resource_type: str
    unit_price: Decimal

    model_config = {"from_attributes": True}


class ContractRebateRequest(BaseModel):
    rebate_percent: Decimal = Field(ge=0, le=100)


# --- Customer: Projects ---


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    description: str = ""
    users: list[str] = Field(default_factory=list)


class UpdateProjectRequest(BaseModel):
    description: str | None = None
    users: list[str] | None = None


class ProjectResponse(BaseModel):
    resource_name: str
    name: str
    description: str
    contract_number: str
    users: list[str]
    phase: str | None = None
    managed: bool = False


# --- Billing Jobs ---


class CreateBillingJobRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    all_contracts: bool = False
    contract_ids: list[int] = Field(default_factory=list)
    schedule: str = Field(min_length=1, max_length=100)
    delivery_method: str = Field(pattern=r"^(webdav|email)$")
    delivery_config: dict
    filename_template: str = Field(default="billing-{year}-{month}.csv", max_length=255)
    per_contract: bool = False
    enabled: bool = True


class UpdateBillingJobRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    all_contracts: bool | None = None
    contract_ids: list[int] | None = None
    schedule: str | None = Field(default=None, min_length=1, max_length=100)
    delivery_method: str | None = Field(default=None, pattern=r"^(webdav|email)$")
    delivery_config: dict | None = None
    filename_template: str | None = Field(default=None, max_length=255)
    per_contract: bool | None = None
    enabled: bool | None = None


class ManualRunRequest(BaseModel):
    year: int | None = None
    month: int | None = None


class BillingJobResponse(BaseModel):
    id: int
    name: str
    owner_sub: str
    all_contracts: bool
    contract_ids: list[int] = []
    schedule: str
    delivery_method: str
    delivery_config: dict
    filename_template: str
    per_contract: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime | None = None


class BillingJobRunResponse(BaseModel):
    id: int
    billing_job_id: int
    started_at: datetime
    completed_at: datetime | None = None
    billing_period_start: datetime
    billing_period_end: datetime
    status: str
    error_message: str | None = None
    files_delivered: int

    model_config = {"from_attributes": True}


# --- Auth ---


class UserInfo(BaseModel):
    sub: str
    name: str | None = None
    email: str | None = None
    is_admin: bool = False
    contracts: list[ContractWithCustomerResponse] = []


# --- Tenant Clusters ---


def _size_label(worker_groups: int) -> str:
    """1=Liten, 2=Mellan, 3=Stor, 4=XL, N>=4 ⇒ (N−3)*'X' + 'L'."""
    table = {1: "Liten", 2: "Mellan", 3: "Stor"}
    if worker_groups in table:
        return table[worker_groups]
    if worker_groups < 1:
        return f"Invalid({worker_groups})"
    return ("X" * (worker_groups - 3)) + "L"


class CreateClusterRequest(BaseModel):
    contract_number: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    api_url: str = Field(min_length=1, max_length=512)
    ca_bundle: str = Field(min_length=1)
    # openbao_mount is implicit: f"kubernetes/{slug}" — set server-side.
    openbao_role: str = Field(default="argocd-rbac-manager", max_length=255)
    argocd_role_name: str = Field(default="argocd-tenant", max_length=255)
    argocd_namespace: str = Field(default="argocd", max_length=63)
    worker_groups: int = Field(default=1, ge=1)


class UpdateClusterRequest(BaseModel):
    # openbao_mount is tied to the (immutable) slug, so it can't be patched.
    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_url: str | None = Field(default=None, min_length=1, max_length=512)
    ca_bundle: str | None = None
    openbao_role: str | None = Field(default=None, max_length=255)
    argocd_role_name: str | None = Field(default=None, max_length=255)
    argocd_namespace: str | None = Field(default=None, max_length=63)


class ClusterResponse(BaseModel):
    id: int
    contract_number: str
    name: str
    slug: str
    api_url: str
    worker_groups: int
    initial_worker_groups: int
    size_label: str
    total_servers: int  # 3 + 3 × worker_groups
    provisioned_at: datetime | None = None
    management_project_resource_name: str | None = None
    backup_project_resource_name: str | None = None
    argocd_namespace: str
    created_at: datetime
    caller_role: str | None = None  # 'sunet_admin' | 'customer_admin' | 'user' | None
    active_addons: list[str] = []


class ClusterAccessRequest(BaseModel):
    user_sub: str = Field(min_length=1, max_length=255)
    role: str = Field(pattern=r"^(customer_admin|user)$")


class ClusterAccessResponse(BaseModel):
    user_sub: str
    role: str
    granted_by_sub: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Kubeconfig issuance ---


class IssueKubeconfigRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class KubeconfigIssuanceResponse(BaseModel):
    id: int
    cluster_slug: str
    user_sub: str
    label: str
    cert_serial: str
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by_sub: str | None = None
    status: str  # 'active' | 'revoked' | 'expired'


class IssuedKubeconfigResponse(KubeconfigIssuanceResponse):
    """Returned only at issue time; carries the one-shot kubeconfig YAML."""

    kubeconfig: str


# --- Cluster requests (addon / resize / backup) ---


class AddonRequestPayload(BaseModel):
    action: str = Field(pattern=r"^(enable|disable)$")
    addon_type: str = Field(min_length=1, max_length=64)


class ResizeRequestPayload(BaseModel):
    target_worker_groups: int = Field(ge=1)


class BackupRequestPayload(BaseModel):
    action: str = Field(pattern=r"^(enable|disable)$")


class CreateClusterRequestRequest(BaseModel):
    """The HTTP body for POST /api/clusters/{slug}/requests."""

    request_type: str = Field(pattern=r"^(addon|resize|backup)$")
    # The shape is validated against request_type in the router.
    payload: dict


class ApplyOrDenyRequestRequest(BaseModel):
    note: str | None = None


class ClusterRequestResponse(BaseModel):
    id: int
    cluster_id: int
    cluster_slug: str
    request_type: str
    payload: dict
    status: str
    requested_by_sub: str
    requested_at: datetime
    applied_by_sub: str | None = None
    applied_at: datetime | None = None
    note: str | None = None
