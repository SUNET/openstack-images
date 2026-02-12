"""OpenStack SDK wrapper with retry logic and connection management."""

import logging
import os
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import openstack
from openstack.connection import Connection
from openstack.exceptions import ConflictException, DuplicateResource, HttpException, ResourceNotFound
from openstack.identity.v3.domain import Domain
from openstack.identity.v3.group import Group
from openstack.identity.v3.project import Project
from openstack.identity.v3.role import Role
from openstack.identity.v3.user import User
from openstack.network.v2.network import Network
from openstack.network.v2.router import Router
from openstack.network.v2.security_group import SecurityGroup
from openstack.network.v2.security_group_rule import SecurityGroupRule
from openstack.network.v2.subnet import Subnet

from models import OpenStackAPIError, ResourceNotFoundError
from metrics import (
    OPENSTACK_API_CALLS,
    OPENSTACK_API_DURATION,
    OPENSTACK_API_RETRIES,
)
from ratelimit import get_rate_limiter

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def _get_service_from_func_name(func_name: str) -> str:
    """Extract OpenStack service name from function name."""
    service_prefixes = {
        "domain": "identity",
        "project": "identity",
        "group": "identity",
        "user": "identity",
        "role": "identity",
        "identity_provider": "identity",
        "mapping": "identity",
        "federation": "identity",
        "network": "network",
        "subnet": "network",
        "router": "network",
        "security_group": "network",
        "flavor": "compute",
        "image": "image",
        "compute_quota": "compute",
        "volume_quota": "block_storage",
        "network_quota": "network",
    }
    func_lower = func_name.lower()
    for prefix, service in service_prefixes.items():
        if prefix in func_lower:
            return service
    return "unknown"


def retry_on_error(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (HttpException,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator to retry operations on transient errors with metrics and rate limiting."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exception: Exception | None = None
            current_delay = delay
            service = _get_service_from_func_name(func.__name__)
            operation = func.__name__
            rate_limiter = get_rate_limiter()

            for attempt in range(max_retries + 1):
                # Acquire rate limit slot before making API call
                with rate_limiter.acquire():
                    start_time = time.monotonic()
                    try:
                        result = func(*args, **kwargs)
                        # Record successful API call
                        duration = time.monotonic() - start_time
                        OPENSTACK_API_CALLS.labels(
                            service=service, operation=operation, status="success"
                        ).inc()
                        OPENSTACK_API_DURATION.labels(
                            service=service, operation=operation
                        ).observe(duration)
                        return result
                    except exceptions as e:
                        last_exception = e
                        duration = time.monotonic() - start_time
                        OPENSTACK_API_DURATION.labels(
                            service=service, operation=operation
                        ).observe(duration)

                        if attempt < max_retries:
                            OPENSTACK_API_RETRIES.labels(
                                service=service, operation=operation
                            ).inc()
                            logger.warning(
                                "Attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                                attempt + 1,
                                max_retries + 1,
                                func.__name__,
                                e,
                                current_delay,
                            )
                        else:
                            OPENSTACK_API_CALLS.labels(
                                service=service, operation=operation, status="error"
                            ).inc()
                            logger.error(
                                "All %d attempts failed for %s",
                                max_retries + 1,
                                func.__name__,
                            )

                # Sleep for retry outside the rate limiter context
                if attempt < max_retries and last_exception is not None:
                    time.sleep(current_delay)
                    current_delay *= backoff

            if last_exception is not None:
                raise OpenStackAPIError(
                    f"Operation {func.__name__} failed after {max_retries + 1} attempts"
                ) from last_exception
            raise OpenStackAPIError(f"Operation {func.__name__} failed unexpectedly")

        return wrapper

    return decorator


class OpenStackClient:
    """Wrapper around OpenStack SDK with convenience methods."""

    def __init__(
        self, cloud: str | None = None, clouds_config: str | None = None
    ) -> None:
        """Initialize OpenStack connection.

        Args:
            cloud: Cloud name from clouds.yaml (default: from OS_CLOUD env)
            clouds_config: Path to clouds.yaml (default: OS_CLIENT_CONFIG_FILE env)
        """
        self.cloud_name = cloud or os.environ.get("OS_CLOUD", "openstack")
        if clouds_config:
            os.environ["OS_CLIENT_CONFIG_FILE"] = clouds_config

        self._conn: Connection | None = None

    @property
    def conn(self) -> Connection:
        """Get or create OpenStack connection."""
        if self._conn is None:
            logger.info("Connecting to OpenStack cloud: %s", self.cloud_name)
            self._conn = openstack.connect(cloud=self.cloud_name)
        return self._conn

    def close(self) -> None:
        """Close the OpenStack connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -------------------------------------------------------------------------
    # Domain operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_domain(self, name_or_id: str) -> Domain | None:
        """Get a domain by name or ID."""
        return self.conn.identity.find_domain(name_or_id)

    def require_domain(self, name_or_id: str) -> Domain:
        """Get a domain, raising if not found."""
        domain = self.get_domain(name_or_id)
        if not domain:
            raise ResourceNotFoundError(f"Domain not found: {name_or_id}")
        return domain

    # -------------------------------------------------------------------------
    # Project operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_project(self, name: str, domain: str) -> Project | None:
        """Get a project by name within a domain."""
        domain_obj = self.get_domain(domain)
        if not domain_obj:
            return None
        return self.conn.identity.find_project(name, domain_id=domain_obj.id)

    def list_projects_in_domain(self, domain_id: str) -> list[Project]:
        """List all projects in a domain."""
        return list(self.conn.identity.projects(domain_id=domain_id))

    def list_projects_with_tag(self, domain_id: str, tag: str) -> list[Project]:
        """List projects in a domain that have a specific tag."""
        return list(self.conn.identity.projects(domain_id=domain_id, tags=tag))

    @retry_on_error()
    def add_project_tag(self, project_id: str, tag: str) -> None:
        """Add a tag to a project."""
        # Get current tags and add new one
        project = self.conn.identity.get_project(project_id)
        current_tags = set(project.tags or [])
        if tag not in current_tags:
            current_tags.add(tag)
            self.conn.identity.update_project(project_id, tags=list(current_tags))
            logger.debug("Added tag %s to project %s", tag, project_id)

    def project_has_tag(self, project_id: str, tag: str) -> bool:
        """Check if a project has a specific tag."""
        project = self.conn.identity.get_project(project_id)
        return tag in (project.tags or [])

    @retry_on_error()
    def create_project(
        self,
        name: str,
        domain: str,
        description: str = "",
        enabled: bool = True,
    ) -> Project:
        """Create a new project."""
        domain_obj = self.require_domain(domain)
        logger.info("Creating project: %s in domain %s", name, domain)
        return self.conn.identity.create_project(
            name=name,
            domain_id=domain_obj.id,
            description=description,
            is_enabled=enabled,
        )

    @retry_on_error()
    def update_project(
        self,
        project_id: str,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> Project:
        """Update an existing project."""
        updates: dict[str, object] = {}
        if description is not None:
            updates["description"] = description
        if enabled is not None:
            updates["is_enabled"] = enabled

        if updates:
            logger.info("Updating project %s: %s", project_id, updates)
            return self.conn.identity.update_project(project_id, **updates)
        return self.conn.identity.get_project(project_id)

    @retry_on_error()
    def delete_project(self, project_id: str) -> None:
        """Delete a project."""
        logger.info("Deleting project: %s", project_id)
        self.conn.identity.delete_project(project_id)

    # -------------------------------------------------------------------------
    # Group operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_group(self, name: str, domain: str) -> Group | None:
        """Get a group by name within a domain."""
        domain_obj = self.get_domain(domain)
        if not domain_obj:
            return None
        return self.conn.identity.find_group(name, domain_id=domain_obj.id)

    @retry_on_error()
    def create_group(self, name: str, domain: str, description: str = "") -> Group:
        """Create a new group."""
        domain_obj = self.require_domain(domain)
        logger.info("Creating group: %s in domain %s", name, domain)
        return self.conn.identity.create_group(
            name=name,
            domain_id=domain_obj.id,
            description=description,
        )

    @retry_on_error()
    def delete_group(self, group_id: str) -> None:
        """Delete a group."""
        logger.info("Deleting group: %s", group_id)
        try:
            self.conn.identity.delete_group(group_id)
        except ResourceNotFound:
            logger.debug("Group %s already deleted", group_id)

    @retry_on_error()
    def get_group_by_id(self, group_id: str) -> Group | None:
        """Get a group by ID."""
        try:
            return self.conn.identity.get_group(group_id)
        except ResourceNotFound:
            return None

    # -------------------------------------------------------------------------
    # User operations
    # -------------------------------------------------------------------------

    def get_user(self, name: str, domain: str) -> User | None:
        """Get a user by name within a domain."""
        domain_obj = self.get_domain(domain)
        if not domain_obj:
            return None
        return self.conn.identity.find_user(name, domain_id=domain_obj.id)

    @retry_on_error()
    def add_user_to_group(self, user_id: str, group_id: str) -> None:
        """Add a user to a group."""
        logger.info("Adding user %s to group %s", user_id, group_id)
        self.conn.identity.add_user_to_group(user_id, group_id)

    @retry_on_error()
    def remove_user_from_group(self, user_id: str, group_id: str) -> None:
        """Remove a user from a group."""
        logger.info("Removing user %s from group %s", user_id, group_id)
        self.conn.identity.remove_user_from_group(user_id, group_id)

    def list_group_users(self, group_id: str) -> list[User]:
        """List all users in a group."""
        return list(self.conn.identity.group_users(group_id))

    # -------------------------------------------------------------------------
    # Role operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_role(self, name: str) -> Role | None:
        """Get a role by name."""
        return self.conn.identity.find_role(name)

    @retry_on_error()
    def assign_role_to_group(
        self, role_id: str, group_id: str, project_id: str
    ) -> None:
        """Assign a role to a group on a project."""
        logger.info(
            "Assigning role %s to group %s on project %s",
            role_id,
            group_id,
            project_id,
        )
        try:
            self.conn.identity.assign_project_role_to_group(
                project=project_id,
                group=group_id,
                role=role_id,
            )
        except ConflictException:
            logger.debug("Role %s already assigned to group %s", role_id, group_id)

    @retry_on_error()
    def revoke_role_from_group(
        self, role_id: str, group_id: str, project_id: str
    ) -> None:
        """Revoke a role from a group on a project."""
        logger.info(
            "Revoking role %s from group %s on project %s",
            role_id,
            group_id,
            project_id,
        )
        try:
            self.conn.identity.unassign_project_role_from_group(
                project=project_id,
                group=group_id,
                role=role_id,
            )
        except ResourceNotFound:
            logger.debug("Role %s not assigned to group %s", role_id, group_id)

    # -------------------------------------------------------------------------
    # Quota operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def set_compute_quotas(self, project_id: str, quotas: dict[str, int]) -> None:
        """Set compute quotas for a project."""
        if not quotas:
            return

        logger.info("Setting compute quotas for project %s: %s", project_id, quotas)
        quota_args: dict[str, int] = {}

        quota_mapping = {
            "instances": "instances",
            "cores": "cores",
            "ramMB": "ram",
            "serverGroups": "server_groups",
            "serverGroupMembers": "server_group_members",
        }

        for spec_key, api_key in quota_mapping.items():
            if spec_key in quotas:
                quota_args[api_key] = quotas[spec_key]

        if quota_args:
            self.conn.compute.update_quota_set(project_id, **quota_args)

    @retry_on_error()
    def set_volume_quotas(self, project_id: str, quotas: dict[str, int]) -> None:
        """Set volume quotas for a project."""
        if not quotas:
            return

        logger.info("Setting volume quotas for project %s: %s", project_id, quotas)
        quota_args: dict[str, int] = {}

        quota_mapping = {
            "volumes": "volumes",
            "volumesGB": "gigabytes",
            "snapshots": "snapshots",
            "backups": "backups",
            "backupsGB": "backup_gigabytes",
        }

        for spec_key, api_key in quota_mapping.items():
            if spec_key in quotas:
                quota_args[api_key] = quotas[spec_key]

        if quota_args:
            self.conn.block_storage.update_quota_set(project_id, **quota_args)

    @retry_on_error()
    def set_network_quotas(self, project_id: str, quotas: dict[str, int]) -> None:
        """Set network quotas for a project."""
        if not quotas:
            return

        logger.info("Setting network quotas for project %s: %s", project_id, quotas)
        quota_args: dict[str, int] = {}

        quota_mapping = {
            "floatingIps": "floatingip",
            "networks": "network",
            "subnets": "subnet",
            "routers": "router",
            "ports": "port",
            "securityGroups": "security_group",
            "securityGroupRules": "security_group_rule",
        }

        for spec_key, api_key in quota_mapping.items():
            if spec_key in quotas:
                quota_args[api_key] = quotas[spec_key]

        if quota_args:
            self.conn.network.update_quota(project_id, **quota_args)

    # -------------------------------------------------------------------------
    # Network operations
    # -------------------------------------------------------------------------

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_network(self, name: str, project_id: str) -> Network | None:
        """Get a network by name within a project."""
        networks = list(self.conn.network.networks(name=name, project_id=project_id))
        return networks[0] if networks else None

    @retry_on_error()
    def create_network(
        self, name: str, project_id: str, tags: list[str] | None = None
    ) -> Network:
        """Create a network."""
        logger.info("Creating network: %s in project %s", name, project_id)
        network = self.conn.network.create_network(name=name, project_id=project_id)
        if tags:
            self.conn.network.set_tags(network, tags)
        return network

    @retry_on_error()
    def delete_network(self, network_id: str) -> None:
        """Delete a network."""
        logger.info("Deleting network: %s", network_id)
        try:
            self.conn.network.delete_network(network_id)
        except ResourceNotFound:
            logger.debug("Network %s already deleted", network_id)

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_subnet(self, name: str, network_id: str) -> Subnet | None:
        """Get a subnet by name within a network."""
        subnets = list(self.conn.network.subnets(name=name, network_id=network_id))
        return subnets[0] if subnets else None

    @retry_on_error(exceptions=(HttpException, KeyError))
    def list_subnets(self, network_id: str) -> list[Subnet]:
        """List all subnets on a network."""
        return list(self.conn.network.subnets(network_id=network_id))

    @retry_on_error()
    def create_subnet(
        self,
        name: str,
        network_id: str,
        cidr: str,
        enable_dhcp: bool = True,
        dns_nameservers: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Subnet:
        """Create a subnet."""
        logger.info("Creating subnet: %s with CIDR %s", name, cidr)
        subnet = self.conn.network.create_subnet(
            name=name,
            network_id=network_id,
            cidr=cidr,
            ip_version=4,
            is_dhcp_enabled=enable_dhcp,
            dns_nameservers=dns_nameservers or [],
        )
        if tags:
            self.conn.network.set_tags(subnet, tags)
        return subnet

    @retry_on_error()
    def delete_subnet(self, subnet_id: str) -> None:
        """Delete a subnet."""
        logger.info("Deleting subnet: %s", subnet_id)
        try:
            self.conn.network.delete_subnet(subnet_id)
        except ResourceNotFound:
            logger.debug("Subnet %s already deleted", subnet_id)

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_router(self, name: str, project_id: str) -> Router | None:
        """Get a router by name within a project."""
        routers = list(self.conn.network.routers(name=name, project_id=project_id))
        return routers[0] if routers else None

    @retry_on_error()
    def create_router(
        self,
        name: str,
        project_id: str,
        external_network_id: str | None = None,
        enable_snat: bool = True,
        tags: list[str] | None = None,
    ) -> Router:
        """Create a router."""
        logger.info("Creating router: %s in project %s", name, project_id)
        external_gateway_info = None
        if external_network_id:
            external_gateway_info = {
                "network_id": external_network_id,
                "enable_snat": enable_snat,
            }
        router = self.conn.network.create_router(
            name=name,
            project_id=project_id,
            external_gateway_info=external_gateway_info,
        )
        if tags:
            self.conn.network.set_tags(router, tags)
        return router

    @retry_on_error()
    def add_router_interface(self, router_id: str, subnet_id: str) -> None:
        """Add a subnet interface to a router."""
        logger.info("Adding interface for subnet %s to router %s", subnet_id, router_id)
        try:
            self.conn.network.add_interface_to_router(router_id, subnet_id=subnet_id)
        except ConflictException:
            logger.debug(
                "Interface for subnet %s already on router %s", subnet_id, router_id
            )

    @retry_on_error()
    def remove_router_interface(self, router_id: str, subnet_id: str) -> None:
        """Remove a subnet interface from a router."""
        logger.info(
            "Removing interface for subnet %s from router %s", subnet_id, router_id
        )
        try:
            self.conn.network.remove_interface_from_router(
                router_id, subnet_id=subnet_id
            )
        except ResourceNotFound:
            logger.debug(
                "Interface for subnet %s not on router %s", subnet_id, router_id
            )

    @retry_on_error()
    def delete_router(self, router_id: str) -> None:
        """Delete a router."""
        logger.info("Deleting router: %s", router_id)
        try:
            self.conn.network.delete_router(router_id)
        except ResourceNotFound:
            logger.debug("Router %s already deleted", router_id)

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_external_network(self, name: str) -> Network | None:
        """Get an external network by name."""
        networks = list(
            self.conn.network.networks(name=name, is_router_external=True)
        )
        return networks[0] if networks else None

    # -------------------------------------------------------------------------
    # Security group operations
    # -------------------------------------------------------------------------

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_security_group(self, name: str, project_id: str) -> SecurityGroup | None:
        """Get a security group by name within a project."""
        sgs = list(
            self.conn.network.security_groups(name=name, project_id=project_id)
        )
        return sgs[0] if sgs else None

    @retry_on_error()
    def create_security_group(
        self,
        name: str,
        project_id: str,
        description: str = "",
        tags: list[str] | None = None,
    ) -> SecurityGroup:
        """Create a security group."""
        logger.info("Creating security group: %s in project %s", name, project_id)
        sg = self.conn.network.create_security_group(
            name=name,
            project_id=project_id,
            description=description,
        )
        if tags:
            self.conn.network.set_tags(sg, tags)
        return sg

    @retry_on_error()
    def delete_security_group(self, sg_id: str) -> None:
        """Delete a security group."""
        logger.info("Deleting security group: %s", sg_id)
        try:
            self.conn.network.delete_security_group(sg_id)
        except ResourceNotFound:
            logger.debug("Security group %s already deleted", sg_id)

    @retry_on_error()
    def create_security_group_rule(
        self,
        security_group_id: str,
        direction: str,
        protocol: str | None = None,
        port_range_min: int | None = None,
        port_range_max: int | None = None,
        remote_ip_prefix: str | None = None,
        remote_group_id: str | None = None,
        ethertype: str = "IPv4",
    ) -> SecurityGroupRule | None:
        """Create a security group rule."""
        logger.info(
            "Creating security group rule: %s %s %s-%s in %s",
            direction,
            protocol,
            port_range_min,
            port_range_max,
            security_group_id,
        )
        try:
            return self.conn.network.create_security_group_rule(
                security_group_id=security_group_id,
                direction=direction,
                protocol=protocol if protocol != "any" else None,
                port_range_min=port_range_min,
                port_range_max=port_range_max,
                remote_ip_prefix=remote_ip_prefix,
                remote_group_id=remote_group_id,
                ether_type=ethertype,
            )
        except ConflictException:
            logger.debug("Security group rule already exists")
            return None

    # -------------------------------------------------------------------------
    # Federation operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_identity_provider(self, idp_id: str) -> object | None:
        """Get an identity provider by ID."""
        try:
            return self.conn.identity.get_identity_provider(idp_id)
        except ResourceNotFound:
            return None

    @retry_on_error()
    def create_identity_provider(
        self,
        idp_id: str,
        remote_ids: list[str],
        domain_id: str | None = None,
    ) -> object:
        """Create an identity provider.

        Args:
            idp_id: Identity provider name/ID
            remote_ids: List of remote IDs (e.g. OIDC issuer URLs)
            domain_id: Domain ID for federated users. If not provided,
                Keystone auto-generates a dedicated federated domain.
        """
        logger.info("Creating identity provider: %s (domain_id=%s)", idp_id, domain_id)
        kwargs: dict = {
            "id": idp_id,
            "remote_ids": remote_ids,
            "is_enabled": True,
        }
        if domain_id:
            kwargs["domain_id"] = domain_id
        return self.conn.identity.create_identity_provider(**kwargs)

    @retry_on_error()
    def get_mapping(self, mapping_id: str) -> object | None:
        """Get a federation mapping by ID."""
        try:
            return self.conn.identity.get_mapping(mapping_id)
        except ResourceNotFound:
            return None

    @retry_on_error()
    def create_mapping(
        self, mapping_id: str, rules: list[dict[str, object]]
    ) -> object:
        """Create a federation mapping."""
        logger.info("Creating mapping: %s", mapping_id)
        return self.conn.identity.create_mapping(id=mapping_id, rules=rules)

    @retry_on_error()
    def update_mapping(
        self, mapping_id: str, rules: list[dict[str, object]]
    ) -> object:
        """Update a federation mapping."""
        logger.info("Updating mapping: %s", mapping_id)
        return self.conn.identity.update_mapping(mapping_id, rules=rules)

    @retry_on_error()
    def get_federation_protocol(
        self, idp_id: str, protocol_id: str
    ) -> object | None:
        """Get a federation protocol."""
        try:
            return self.conn.identity.get_federation_protocol(idp_id, protocol_id)
        except ResourceNotFound:
            return None

    @retry_on_error()
    def create_federation_protocol(
        self, idp_id: str, protocol_id: str, mapping_id: str
    ) -> object:
        """Create a federation protocol."""
        logger.info("Creating federation protocol: %s for IdP %s", protocol_id, idp_id)
        return self.conn.identity.create_federation_protocol(
            idp_id,
            id=protocol_id,
            mapping_id=mapping_id,
        )

    # -------------------------------------------------------------------------
    # Domain management operations (create/update/delete)
    # -------------------------------------------------------------------------

    @retry_on_error()
    def create_domain(
        self,
        name: str,
        description: str = "",
        enabled: bool = True,
    ) -> Domain:
        """Create a new domain."""
        logger.info("Creating domain: %s", name)
        return self.conn.identity.create_domain(
            name=name,
            description=description,
            is_enabled=enabled,
        )

    @retry_on_error()
    def update_domain(
        self,
        domain_id: str,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> Domain:
        """Update an existing domain."""
        updates: dict[str, object] = {}
        if description is not None:
            updates["description"] = description
        if enabled is not None:
            updates["is_enabled"] = enabled

        if updates:
            logger.info("Updating domain %s: %s", domain_id, updates)
            return self.conn.identity.update_domain(domain_id, **updates)
        return self.conn.identity.get_domain(domain_id)

    @retry_on_error()
    def delete_domain(self, domain_id: str) -> None:
        """Delete a domain. Domain must be disabled first."""
        logger.info("Deleting domain: %s", domain_id)
        try:
            # Ensure domain is disabled before deletion
            self.conn.identity.update_domain(domain_id, is_enabled=False)
            self.conn.identity.delete_domain(domain_id)
        except ResourceNotFound:
            logger.debug("Domain %s already deleted", domain_id)

    # -------------------------------------------------------------------------
    # Flavor operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_flavor(self, name: str) -> object | None:
        """Get a flavor by name."""
        return self.conn.compute.find_flavor(name)

    @retry_on_error()
    def create_flavor(
        self,
        name: str,
        vcpus: int,
        ram: int,
        disk: int = 0,
        ephemeral: int = 0,
        swap: int = 0,
        is_public: bool = True,
        description: str = "",
    ) -> object:
        """Create a new flavor."""
        logger.info("Creating flavor: %s (vcpus=%d, ram=%d, disk=%d)", name, vcpus, ram, disk)
        return self.conn.compute.create_flavor(
            name=name,
            vcpus=vcpus,
            ram=ram,
            disk=disk,
            ephemeral=ephemeral,
            swap=swap,
            is_public=is_public,
            description=description,
        )

    @retry_on_error()
    def set_flavor_extra_specs(self, flavor_id: str, extra_specs: dict[str, str]) -> None:
        """Set extra specs on a flavor."""
        if not extra_specs:
            return
        logger.info("Setting extra specs on flavor %s: %s", flavor_id, extra_specs)
        self.conn.compute.create_flavor_extra_specs(flavor_id, extra_specs)

    @retry_on_error()
    def delete_flavor(self, flavor_id: str) -> None:
        """Delete a flavor."""
        logger.info("Deleting flavor: %s", flavor_id)
        try:
            self.conn.compute.delete_flavor(flavor_id)
        except ResourceNotFound:
            logger.debug("Flavor %s already deleted", flavor_id)

    # -------------------------------------------------------------------------
    # Image operations
    # -------------------------------------------------------------------------

    @retry_on_error()
    def get_image(self, name: str) -> object | None:
        """Get an image by name.

        If multiple images exist with the same name, returns the first one
        and logs a warning.
        """
        try:
            return self.conn.image.find_image(name)
        except DuplicateResource:
            # Multiple images with same name - list them and return the first
            images = list(self.conn.image.images(name=name))
            if images:
                logger.warning(
                    "Multiple images found with name '%s' (count=%d), using first: %s",
                    name,
                    len(images),
                    images[0].id,
                )
                return images[0]
            return None

    @retry_on_error()
    def create_image(
        self,
        name: str,
        disk_format: str,
        container_format: str = "bare",
        visibility: str = "private",
        protected: bool = False,
        tags: list[str] | None = None,
        properties: dict[str, str] | None = None,
    ) -> object:
        """Create a new image (metadata only, no data uploaded yet)."""
        logger.info("Creating image: %s (disk_format=%s, visibility=%s)", name, disk_format, visibility)
        kwargs: dict[str, object] = {
            "name": name,
            "disk_format": disk_format,
            "container_format": container_format,
            "visibility": visibility,
            "is_protected": protected,
        }
        if tags:
            kwargs["tags"] = tags
        if properties:
            # Properties are passed directly to the image
            kwargs.update(properties)

        return self.conn.image.create_image(**kwargs)

    @retry_on_error()
    def import_image_from_url(self, image_id: str, url: str) -> None:
        """Import image data from a URL using Glance web-download.

        This initiates an async download in Glance. The image status
        should be polled to check completion.
        """
        logger.info("Importing image %s from URL: %s", image_id, url)
        # Fetch the full image object first — openstacksdk's import_image
        # validates container_format/disk_format client-side, but when given
        # just a string ID it creates a skeleton object with those fields as
        # None, causing a spurious InvalidRequest error.
        image = self.conn.image.get_image(image_id)
        self.conn.image.import_image(
            image,
            method="web-download",
            uri=url,
        )

    @retry_on_error()
    def get_image_by_id(self, image_id: str) -> object | None:
        """Get an image by ID."""
        try:
            return self.conn.image.get_image(image_id)
        except ResourceNotFound:
            return None

    @retry_on_error()
    def update_image(
        self,
        image_id: str,
        visibility: str | None = None,
        protected: bool | None = None,
        tags: list[str] | None = None,
        properties: dict[str, str] | None = None,
    ) -> object:
        """Update an existing image."""
        kwargs: dict[str, object] = {}
        if visibility is not None:
            kwargs["visibility"] = visibility
        if protected is not None:
            kwargs["is_protected"] = protected
        if tags is not None:
            kwargs["tags"] = tags
        if properties:
            kwargs.update(properties)

        if kwargs:
            logger.info("Updating image %s: %s", image_id, kwargs)
            return self.conn.image.update_image(image_id, **kwargs)
        return self.conn.image.get_image(image_id)

    @retry_on_error()
    def delete_image(self, image_id: str) -> None:
        """Delete an image."""
        logger.info("Deleting image: %s", image_id)
        try:
            # Unprotect image first if needed
            image = self.conn.image.get_image(image_id)
            if image and getattr(image, "is_protected", False):
                self.conn.image.update_image(image_id, is_protected=False)
            self.conn.image.delete_image(image_id)
        except ResourceNotFound:
            logger.debug("Image %s already deleted", image_id)

    # -------------------------------------------------------------------------
    # Provider network operations (admin)
    # -------------------------------------------------------------------------

    @retry_on_error(exceptions=(HttpException, KeyError))
    def get_network_by_name(self, name: str) -> Network | None:
        """Get a network by name (any project, for provider networks).

        Note: KeyError is included in retry exceptions because the OpenStack SDK
        can raise KeyError when parsing malformed API responses (e.g., when a
        proxy returns an error page instead of JSON).
        """
        try:
            networks = list(self.conn.network.networks(name=name))
            return networks[0] if networks else None
        except KeyError as e:
            # Log debug info to help diagnose API issues
            try:
                endpoint = self.conn.network.get_endpoint()
                logger.error(
                    "KeyError '%s' when listing networks. Endpoint: %s",
                    e,
                    endpoint,
                )
            except Exception:
                pass
            raise

    @retry_on_error()
    def create_provider_network(
        self,
        name: str,
        network_type: str,
        physical_network: str | None = None,
        segmentation_id: int | None = None,
        external: bool = False,
        shared: bool = False,
        description: str = "",
    ) -> Network:
        """Create a provider network (requires admin)."""
        logger.info(
            "Creating provider network: %s (type=%s, physical=%s, external=%s)",
            name, network_type, physical_network, external
        )
        kwargs: dict[str, object] = {
            "name": name,
            "description": description,
            "is_router_external": external,
            "is_shared": shared,
            "provider_network_type": network_type,
        }
        if physical_network:
            kwargs["provider_physical_network"] = physical_network
        if segmentation_id:
            kwargs["provider_segmentation_id"] = segmentation_id

        return self.conn.network.create_network(**kwargs)

    @retry_on_error()
    def create_subnet_with_pools(
        self,
        name: str,
        network_id: str,
        cidr: str,
        gateway_ip: str | None = None,
        enable_dhcp: bool = True,
        dns_nameservers: list[str] | None = None,
        allocation_pools: list[dict[str, str]] | None = None,
    ) -> Subnet:
        """Create a subnet with allocation pools."""
        logger.info("Creating subnet: %s with CIDR %s (gateway=%s)", name, cidr, gateway_ip)
        kwargs: dict[str, object] = {
            "name": name,
            "network_id": network_id,
            "cidr": cidr,
            "ip_version": 4,
            "is_dhcp_enabled": enable_dhcp,
            "dns_nameservers": dns_nameservers or [],
        }
        if gateway_ip:
            kwargs["gateway_ip"] = gateway_ip
        if allocation_pools:
            kwargs["allocation_pools"] = allocation_pools

        return self.conn.network.create_subnet(**kwargs)
