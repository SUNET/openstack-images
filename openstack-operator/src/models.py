"""Domain models for the OpenStack operator.

This module defines typed data structures for all operator concepts,
making illegal states unrepresentable at the type level.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal, TypedDict, NotRequired


# =============================================================================
# Enums for constrained values
# =============================================================================


class Phase(Enum):
    """Project lifecycle phase."""

    PENDING = "Pending"
    PROVISIONING = "Provisioning"
    READY = "Ready"
    ERROR = "Error"
    DELETING = "Deleting"


class Direction(Enum):
    """Security group rule direction."""

    INGRESS = "ingress"
    EGRESS = "egress"


class Protocol(Enum):
    """Security group rule protocol."""

    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"
    ANY = "any"


class ConditionStatus(Enum):
    """Kubernetes condition status."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


# =============================================================================
# TypedDicts for CRD spec (external data from Kubernetes)
# =============================================================================


class ComputeQuotaSpec(TypedDict, total=False):
    """Compute quota specification from CRD."""

    instances: int
    cores: int
    ramMB: int
    serverGroups: int
    serverGroupMembers: int


class StorageQuotaSpec(TypedDict, total=False):
    """Storage quota specification from CRD."""

    volumes: int
    volumesGB: int
    snapshots: int
    backups: int
    backupsGB: int


class NetworkQuotaSpec(TypedDict, total=False):
    """Network quota specification from CRD."""

    floatingIps: int
    networks: int
    subnets: int
    routers: int
    ports: int
    securityGroups: int
    securityGroupRules: int


class QuotaSpec(TypedDict, total=False):
    """Combined quota specification from CRD."""

    compute: ComputeQuotaSpec
    storage: StorageQuotaSpec
    network: NetworkQuotaSpec


class RouterSpec(TypedDict, total=False):
    """Router specification from CRD."""

    externalNetwork: str
    enableSnat: bool


class NetworkSpec(TypedDict):
    """Network specification from CRD."""

    name: str
    cidr: str
    enableDhcp: NotRequired[bool]
    dnsNameservers: NotRequired[list[str]]
    router: NotRequired[RouterSpec]


class SecurityGroupRuleSpec(TypedDict):
    """Security group rule specification from CRD."""

    direction: Literal["ingress", "egress"]
    protocol: NotRequired[Literal["tcp", "udp", "icmp", "any"]]
    portRangeMin: NotRequired[int]
    portRangeMax: NotRequired[int]
    remoteIpPrefix: NotRequired[str]
    remoteGroupId: NotRequired[str]
    ethertype: NotRequired[Literal["IPv4", "IPv6"]]


class SecurityGroupSpec(TypedDict):
    """Security group specification from CRD."""

    name: str
    description: NotRequired[str]
    rules: NotRequired[list[SecurityGroupRuleSpec]]


class RoleBindingSpec(TypedDict):
    """Role binding specification from CRD."""

    role: str
    users: NotRequired[list[str]]
    groups: NotRequired[list[str]]
    userDomain: NotRequired[str]


class FederationRefSpec(TypedDict, total=False):
    """Federation reference specification from CRD."""

    configMapName: str
    configMapNamespace: str


class OpenstackProjectSpec(TypedDict):
    """Full OpenstackProject CRD spec."""

    name: str
    domain: str
    description: NotRequired[str]
    enabled: NotRequired[bool]
    quotas: NotRequired[QuotaSpec]
    networks: NotRequired[list[NetworkSpec]]
    securityGroups: NotRequired[list[SecurityGroupSpec]]
    roleBindings: NotRequired[list[RoleBindingSpec]]
    federationRef: NotRequired[FederationRefSpec]


# =============================================================================
# TypedDicts for new CRDs (Domain, Flavor, Image, Network)
# =============================================================================


class OpenstackDomainSpec(TypedDict):
    """Full OpenstackDomain CRD spec."""

    name: str
    description: NotRequired[str]
    enabled: NotRequired[bool]


class OpenstackFlavorSpec(TypedDict):
    """Full OpenstackFlavor CRD spec."""

    name: str
    description: NotRequired[str]
    vcpus: int
    ram: int
    disk: NotRequired[int]
    ephemeral: NotRequired[int]
    swap: NotRequired[int]
    isPublic: NotRequired[bool]
    extraSpecs: NotRequired[dict[str, str]]


class ImageSourceSpec(TypedDict):
    """Image source specification from CRD."""

    url: str


class ImageContentSpec(TypedDict):
    """Image content specification from CRD."""

    diskFormat: Literal["raw", "qcow2", "vhd", "vhdx", "vmdk", "vdi", "iso", "aki", "ari", "ami"]
    containerFormat: NotRequired[Literal["bare", "ovf", "ova", "aki", "ari", "ami", "docker"]]
    source: ImageSourceSpec


class OpenstackImageSpec(TypedDict):
    """Full OpenstackImage CRD spec."""

    name: str
    visibility: NotRequired[Literal["public", "private", "shared", "community"]]
    protected: NotRequired[bool]
    tags: NotRequired[list[str]]
    properties: NotRequired[dict[str, str]]
    content: ImageContentSpec


class AllocationPoolSpec(TypedDict):
    """Allocation pool specification for subnets."""

    start: str
    end: str


class ProviderSubnetSpec(TypedDict):
    """Subnet specification for provider networks."""

    name: str
    cidr: str
    gatewayIp: NotRequired[str]
    enableDhcp: NotRequired[bool]
    dnsNameservers: NotRequired[list[str]]
    allocationPools: NotRequired[list[AllocationPoolSpec]]


class OpenstackNetworkSpec(TypedDict):
    """Full OpenstackNetwork CRD spec (provider networks)."""

    name: str
    description: NotRequired[str]
    external: NotRequired[bool]
    shared: NotRequired[bool]
    providerNetworkType: NotRequired[Literal["flat", "vlan", "vxlan", "gre", "geneve"]]
    providerPhysicalNetwork: NotRequired[str]
    providerSegmentationId: NotRequired[int]
    subnets: NotRequired[list[ProviderSubnetSpec]]


# =============================================================================
# Dataclasses for internal state and status
# =============================================================================


@dataclass(frozen=True)
class NetworkStatus:
    """Status of a created network."""

    name: str
    network_id: str
    subnet_id: str
    router_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for Kubernetes status."""
        result = {
            "name": self.name,
            "networkId": self.network_id,
            "subnetId": self.subnet_id,
        }
        if self.router_id:
            result["routerId"] = self.router_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "NetworkStatus":
        """Create from Kubernetes status dict."""
        return cls(
            name=data.get("name", ""),
            network_id=data.get("networkId", ""),
            subnet_id=data.get("subnetId", ""),
            router_id=data.get("routerId"),
        )


@dataclass(frozen=True)
class SecurityGroupStatus:
    """Status of a created security group."""

    name: str
    id: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for Kubernetes status."""
        return {"name": self.name, "id": self.id}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "SecurityGroupStatus":
        """Create from Kubernetes status dict."""
        return cls(name=data.get("name", ""), id=data.get("id", ""))


@dataclass(frozen=True)
class Condition:
    """Kubernetes-style condition."""

    type: str
    status: ConditionStatus
    reason: str = ""
    message: str = ""
    last_transition_time: str = ""

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for Kubernetes status."""
        return {
            "type": self.type,
            "status": self.status.value,
            "reason": self.reason,
            "message": self.message,
            "lastTransitionTime": self.last_transition_time,
        }


@dataclass
class ProjectStatus:
    """Status of an OpenstackProject resource."""

    phase: Phase = Phase.PENDING
    project_id: str | None = None
    group_id: str | None = None
    networks: list[NetworkStatus] = field(default_factory=list)
    security_groups: list[SecurityGroupStatus] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    last_sync_time: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dict for Kubernetes status."""
        result: dict[str, object] = {"phase": self.phase.value}
        if self.project_id:
            result["projectId"] = self.project_id
        if self.group_id:
            result["groupId"] = self.group_id
        if self.networks:
            result["networks"] = [n.to_dict() for n in self.networks]
        if self.security_groups:
            result["securityGroups"] = [sg.to_dict() for sg in self.security_groups]
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in self.conditions]
        if self.last_sync_time:
            result["lastSyncTime"] = self.last_sync_time
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ProjectStatus":
        """Create from Kubernetes status dict."""
        phase_str = data.get("phase", "Pending")
        try:
            phase = Phase(phase_str)
        except ValueError:
            phase = Phase.PENDING

        networks = [
            NetworkStatus.from_dict(n) for n in data.get("networks", []) or []
        ]
        security_groups = [
            SecurityGroupStatus.from_dict(sg)
            for sg in data.get("securityGroups", []) or []
        ]

        return cls(
            phase=phase,
            project_id=data.get("projectId"),  # type: ignore[arg-type]
            group_id=data.get("groupId"),  # type: ignore[arg-type]
            networks=networks,
            security_groups=security_groups,
            last_sync_time=data.get("lastSyncTime"),  # type: ignore[arg-type]
        )

    def set_condition(
        self,
        condition_type: str,
        status: ConditionStatus,
        reason: str = "",
        message: str = "",
    ) -> None:
        """Set or update a condition."""
        from utils import now_iso

        now = now_iso()

        for i, cond in enumerate(self.conditions):
            if cond.type == condition_type:
                if cond.status != status:
                    self.conditions[i] = Condition(
                        type=condition_type,
                        status=status,
                        reason=reason,
                        message=message,
                        last_transition_time=now,
                    )
                else:
                    self.conditions[i] = Condition(
                        type=condition_type,
                        status=status,
                        reason=reason,
                        message=message,
                        last_transition_time=cond.last_transition_time,
                    )
                return

        self.conditions.append(
            Condition(
                type=condition_type,
                status=status,
                reason=reason,
                message=message,
                last_transition_time=now,
            )
        )


@dataclass(frozen=True)
class FederationConfig:
    """Federation configuration loaded from ConfigMap."""

    idp_name: str
    idp_remote_id: str
    sso_domain: str

    @classmethod
    def from_configmap_data(cls, data: dict[str, str]) -> "FederationConfig":
        """Create from ConfigMap data."""
        idp_name = data.get("idp-name", "")
        if not idp_name:
            raise ValueError("idp-name is required in federation config")
        return cls(
            idp_name=idp_name,
            idp_remote_id=data.get("idp-remote-id", ""),
            sso_domain=data.get("sso-domain", ""),
        )


# =============================================================================
# Status dataclasses for new CRDs
# =============================================================================


@dataclass
class DomainStatus:
    """Status of an OpenstackDomain resource."""

    phase: Phase = Phase.PENDING
    domain_id: str | None = None
    conditions: list[Condition] = field(default_factory=list)
    last_sync_time: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dict for Kubernetes status."""
        result: dict[str, object] = {"phase": self.phase.value}
        if self.domain_id:
            result["domainId"] = self.domain_id
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in self.conditions]
        if self.last_sync_time:
            result["lastSyncTime"] = self.last_sync_time
        return result


@dataclass
class FlavorStatus:
    """Status of an OpenstackFlavor resource."""

    phase: Phase = Phase.PENDING
    flavor_id: str | None = None
    conditions: list[Condition] = field(default_factory=list)
    last_sync_time: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dict for Kubernetes status."""
        result: dict[str, object] = {"phase": self.phase.value}
        if self.flavor_id:
            result["flavorId"] = self.flavor_id
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in self.conditions]
        if self.last_sync_time:
            result["lastSyncTime"] = self.last_sync_time
        return result


class ImageUploadStatus(Enum):
    """Glance image status."""

    QUEUED = "queued"
    SAVING = "saving"
    ACTIVE = "active"
    KILLED = "killed"
    DELETED = "deleted"
    PENDING_DELETE = "pending_delete"
    DEACTIVATED = "deactivated"
    UPLOADING = "uploading"
    IMPORTING = "importing"


@dataclass
class ImageStatus:
    """Status of an OpenstackImage resource."""

    phase: Phase = Phase.PENDING
    image_id: str | None = None
    upload_status: str | None = None
    checksum: str | None = None
    size_bytes: int | None = None
    conditions: list[Condition] = field(default_factory=list)
    last_sync_time: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dict for Kubernetes status."""
        result: dict[str, object] = {"phase": self.phase.value}
        if self.image_id:
            result["imageId"] = self.image_id
        if self.upload_status:
            result["uploadStatus"] = self.upload_status
        if self.checksum:
            result["checksum"] = self.checksum
        if self.size_bytes is not None:
            result["sizeBytes"] = self.size_bytes
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in self.conditions]
        if self.last_sync_time:
            result["lastSyncTime"] = self.last_sync_time
        return result


@dataclass(frozen=True)
class ProviderSubnetStatus:
    """Status of a subnet in a provider network."""

    name: str
    subnet_id: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for Kubernetes status."""
        return {"name": self.name, "subnetId": self.subnet_id}


@dataclass
class ProviderNetworkStatus:
    """Status of an OpenstackNetwork resource."""

    phase: Phase = Phase.PENDING
    network_id: str | None = None
    subnets: list[ProviderSubnetStatus] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    last_sync_time: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dict for Kubernetes status."""
        result: dict[str, object] = {"phase": self.phase.value}
        if self.network_id:
            result["networkId"] = self.network_id
        if self.subnets:
            result["subnets"] = [s.to_dict() for s in self.subnets]
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in self.conditions]
        if self.last_sync_time:
            result["lastSyncTime"] = self.last_sync_time
        return result


# =============================================================================
# Exceptions
# =============================================================================


class OperatorError(Exception):
    """Base exception for operator errors."""

    pass


class ResourceNotFoundError(OperatorError):
    """A required OpenStack resource was not found."""

    pass


class ConfigurationError(OperatorError):
    """Invalid or missing configuration."""

    pass


class OpenStackAPIError(OperatorError):
    """Error communicating with OpenStack API."""

    pass
