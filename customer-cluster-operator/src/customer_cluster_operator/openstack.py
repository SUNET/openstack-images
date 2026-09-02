"""Idempotent OpenStack provisioning with strict cluster ownership checks."""

from __future__ import annotations

import base64
import ipaddress
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import openstack
import yaml
from openstack.config import OpenStackConfig
from openstack.exceptions import ConflictException

from .errors import OwnershipError, ValidationError


def stable_name(slug: str, suffix: str) -> str:
    return f"cc-{slug[: 59 - len(suffix)].rstrip('-')}-{suffix}"


def _exact(resources: Iterable[Any], name: str, kind: str) -> Any | None:
    matches = [item for item in resources if item.name == name]
    if len(matches) > 1:
        raise OwnershipError(f"multiple {kind} resources named {name} exist")
    return matches[0] if matches else None


def _tag(uid: str) -> str:
    return f"customer-cluster-uid={uid}"


def _owned(resource: Any, uid: str) -> bool:
    if _tag(uid) in (getattr(resource, "tags", None) or []):
        return True
    metadata = getattr(resource, "metadata", None) or {}
    return metadata.get("customer_cluster_uid") == uid


def _require_owned(resource: Any, uid: str, kind: str) -> Any:
    if not _owned(resource, uid):
        identity = getattr(resource, "name", None) or getattr(resource, "id", "unknown")
        raise OwnershipError(
            f"retained conflicting {kind} {identity} belongs to another cluster UID; "
            "automatic adoption is disabled. Restore the original ManagedCluster UID, "
            "choose a new cluster slug, or manually resolve the retained resource after review"
        )
    return resource


def _server_uses_flavor(server: Any, flavor: Any) -> bool:
    """Match Nova server flavor responses with or without a flavor UUID."""
    embedded = getattr(server, "flavor", None) or {}
    flavor_id = getattr(server, "flavor_id", None)
    if flavor_id:
        return flavor_id == flavor.id
    original_name = (
        embedded.get("original_name")
        if isinstance(embedded, dict)
        else getattr(embedded, "original_name", None)
    )
    if original_name:
        return original_name == flavor.name
    embedded_id = (
        embedded.get("id") if isinstance(embedded, dict) else getattr(embedded, "id", None)
    )
    return bool(embedded_id) and embedded_id == flavor.id


def read_public_keys(path: Path) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise ValidationError(f"cannot read mounted SSH public keys: {exc}") from exc
    keys = []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if "PRIVATE KEY" in value or not value.startswith(("ssh-", "ecdsa-", "sk-")):
            raise ValidationError("SSH bootstrap input must contain public keys only")
        parts = value.split()
        if len(parts) < 2:
            raise ValidationError("invalid SSH public key")
        try:
            base64.b64decode(parts[1], validate=True)
        except ValueError as exc:
            raise ValidationError("invalid SSH public key encoding") from exc
        keys.append(value)
    if not keys:
        raise ValidationError("at least one SSH public key is required")
    return keys


def scoped_connection(data: dict[str, Any], clouds_file: str) -> Any:
    """Build and verify a connection scoped to the requested Keystone project."""
    cloud_name = data["openstack"]["cloud"]
    loader = OpenStackConfig(config_files=[clouds_file])
    configured = loader.cloud_config.get("clouds", {}).get(cloud_name)
    if not isinstance(configured, dict):
        raise ValidationError(f"cloud {cloud_name} does not exist")
    auth = dict(configured.get("auth") or {})
    if not auth:
        raise ValidationError(f"cloud {cloud_name} has no auth configuration")
    auth["project_id"] = data["project"]["id"]
    auth.pop("project_name", None)
    auth.pop("system_scope", None)
    configured["auth"] = auth
    cloud = loader.get_one(cloud=cloud_name)
    connection = openstack.connection.Connection(config=cloud)
    connection.authorize()
    scoped_id = connection.current_project_id
    if scoped_id != data["project"]["id"]:
        raise ValidationError("cloud credentials could not be scoped to the requested projectId")
    project = connection.identity.get_project(data["project"]["id"])
    if project is None or project.name != data["project"]["name"]:
        raise ValidationError("projectId/projectName does not match the authenticated project")
    return connection


class Provisioner:
    """Ensure all infrastructure for one immutable ManagedCluster input."""

    def __init__(self, connection: Any, data: dict[str, Any], public_keys: list[str]):
        self.conn = connection
        self.data = data
        self.uid = data["cluster"]["uid"]
        self.slug = data["cluster"]["slug"]
        self.public_keys = public_keys
        self.tags = [
            "managed-by=customer-cluster-operator",
            _tag(self.uid),
            f"customer-cluster-slug={self.slug}",
        ]
        self.metadata = {
            "managed_by": "customer-cluster-operator",
            "customer_cluster_uid": self.uid,
            "customer_cluster_slug": self.slug,
        }

    def _set_network_tags(self, resource: Any) -> Any:
        """Tag a newly created Neutron resource through the tags endpoint."""
        return self.conn.network.set_tags(resource, self.tags)

    def _network(self) -> Any:
        name = stable_name(self.slug, "network")
        resource = _exact(self.conn.network.networks(name=name), name, "network")
        if resource:
            resource = _require_owned(resource, self.uid, "network")
        else:
            resource = self.conn.network.create_network(name=name)
            resource = self._set_network_tags(resource)
        return resource

    def _subnet(self, network: Any) -> Any:
        name = stable_name(self.slug, "subnet")
        resource = _exact(self.conn.network.subnets(name=name), name, "subnet")
        if resource:
            _require_owned(resource, self.uid, "subnet")
            if (
                resource.network_id != network.id
                or resource.cidr != self.data["network"]["cidr"]
                or resource.ip_version != 4
                or resource.is_dhcp_enabled is not True
                or set(resource.dns_nameservers or [])
                != set(self.data["network"]["dnsNameservers"])
            ):
                raise OwnershipError(f"owned subnet {name} has incompatible configuration")
            return resource
        resource = self.conn.network.create_subnet(
            name=name,
            network_id=network.id,
            cidr=self.data["network"]["cidr"],
            ip_version=4,
            enable_dhcp=True,
            dns_nameservers=self.data["network"]["dnsNameservers"],
        )
        return self._set_network_tags(resource)

    def _external_network(self) -> Any:
        name = self.data["openstack"]["externalNetwork"]
        network = _exact(self.conn.network.networks(name=name), name, "external network")
        if not network or not network.is_router_external:
            raise ValidationError(f"external network {name} does not exist or is not external")
        return network

    def _router(self, subnet: Any, external: Any) -> Any:
        name = stable_name(self.slug, "router")
        resource = _exact(self.conn.network.routers(name=name), name, "router")
        if resource:
            resource = _require_owned(resource, self.uid, "router")
            gateway = resource.external_gateway_info or {}
            if gateway.get("network_id") != external.id or gateway.get("enable_snat") is not True:
                raise OwnershipError(f"owned router {name} uses a different external network")
            interfaces = {
                fixed.get("subnet_id")
                for port in self.conn.network.ports(device_id=resource.id)
                if port.device_owner == "network:router_interface"
                for fixed in port.fixed_ips
            }
            if not interfaces:
                self.conn.network.add_interface_to_router(resource, subnet_id=subnet.id)
            elif interfaces != {subnet.id}:
                raise OwnershipError(
                    f"owned router {name} has unexpected internal subnet interfaces"
                )
        else:
            resource = self.conn.network.create_router(
                name=name,
                external_gateway_info={"network_id": external.id},
            )
            resource = self._set_network_tags(resource)
            self.conn.network.add_interface_to_router(resource, subnet_id=subnet.id)
        return resource

    def _security_group(self, suffix: str) -> Any:
        name = stable_name(self.slug, suffix)
        resource = _exact(self.conn.network.security_groups(name=name), name, "security group")
        if resource:
            return _require_owned(resource, self.uid, "security group")
        resource = self.conn.network.create_security_group(
            name=name,
            description=f"ManagedCluster {self.slug} ({self.uid}) {suffix}",
        )
        return self._set_network_tags(resource)

    def _rule(self, security_group: Any, **desired: Any) -> None:
        keys = (
            "direction",
            "ether_type",
            "protocol",
            "port_range_min",
            "port_range_max",
            "remote_ip_prefix",
            "remote_group_id",
        )
        normalized = {key: desired.get(key) for key in keys}
        for rule in self.conn.network.security_group_rules(security_group_id=security_group.id):
            if all(getattr(rule, key, None) == value for key, value in normalized.items()):
                return
        try:
            self.conn.network.create_security_group_rule(
                security_group_id=security_group.id, **desired
            )
        except ConflictException:
            # Neutron guarantees this response means an equivalent rule exists.
            return

    def _security_groups(self) -> tuple[Any, Any]:
        cluster = self._security_group("cluster-sg")
        jump = self._security_group("jumphost-sg")
        self._rule(
            cluster,
            direction="ingress",
            ether_type="IPv4",
            remote_group_id=cluster.id,
        )
        self._rule(
            cluster,
            direction="ingress",
            ether_type="IPv4",
            protocol="tcp",
            port_range_min=22,
            port_range_max=22,
            remote_group_id=jump.id,
        )
        for cidr in self.data["network"]["sshAllowedCIDRs"]:
            self._rule(
                jump,
                direction="ingress",
                ether_type="IPv4",
                protocol="tcp",
                port_range_min=22,
                port_range_max=22,
                remote_ip_prefix=cidr,
            )
        allowed_cluster = {
            ("IPv4", None, None, None, cluster.id),
            ("IPv4", "tcp", 22, 22, jump.id),
        }
        actual_cluster = {
            (
                getattr(rule, "ether_type", None),
                getattr(rule, "protocol", None),
                getattr(rule, "port_range_min", None),
                getattr(rule, "port_range_max", None),
                getattr(rule, "remote_group_id", None),
            )
            for rule in self.conn.network.security_group_rules(security_group_id=cluster.id)
            if rule.direction == "ingress"
        }
        allowed_jump = {
            ("IPv4", "tcp", 22, 22, cidr) for cidr in self.data["network"]["sshAllowedCIDRs"]
        }
        actual_jump = {
            (
                getattr(rule, "ether_type", None),
                getattr(rule, "protocol", None),
                getattr(rule, "port_range_min", None),
                getattr(rule, "port_range_max", None),
                getattr(rule, "remote_ip_prefix", None),
            )
            for rule in self.conn.network.security_group_rules(security_group_id=jump.id)
            if rule.direction == "ingress"
        }
        if actual_cluster - allowed_cluster or actual_jump - allowed_jump:
            raise OwnershipError("managed security group contains unexpected IPv4 ingress rules")
        return cluster, jump

    def _keypair(self) -> Any:
        name = stable_name(self.slug, f"bootstrap-{self.uid.replace('-', '')[:8]}")
        existing = _exact(self.conn.compute.keypairs(name=name), name, "keypair")
        first = " ".join(self.public_keys[0].split()[:2])
        if existing:
            existing_key = " ".join((existing.public_key or "").split()[:2])
            if existing_key != first:
                raise OwnershipError(f"keypair {name} has a different public key")
            return existing
        return self.conn.compute.create_keypair(name=name, public_key=self.public_keys[0])

    def _image(self) -> Any:
        name = self.data["openstack"]["image"]
        image = _exact(self.conn.image.images(name=name), name, "image")
        if not image:
            raise ValidationError(f"image {name} does not exist")
        distro = str(getattr(image, "os_distro", "")).lower()
        version = str(getattr(image, "os_version", "")).lower()
        searchable = " ".join([name.lower(), version, *[str(x).lower() for x in image.tags or []]])
        if distro != "debian" or not any(value in searchable for value in ("13", "trixie")):
            raise ValidationError(f"image {name} is not identified as Debian Trixie")
        return image

    def _flavor(self, name: str) -> Any:
        flavor = _exact(self.conn.compute.flavors(name=name), name, "flavor")
        if not flavor:
            raise ValidationError(f"flavor {name} does not exist")
        return flavor

    def _cloud_init(self) -> str:
        document = {
            "disable_root": False,
            "ssh_pwauth": False,
            "users": [
                "default",
                {
                    "name": "root",
                    "lock_passwd": True,
                    "ssh_authorized_keys": self.public_keys,
                },
            ],
        }
        return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)

    def _verify_volume(
        self, volume: Any, size: int, image: Any, expected_server_id: str | None
    ) -> Any:
        _require_owned(volume, self.uid, "volume")
        metadata = volume.metadata or {}
        image_metadata = volume.volume_image_metadata or {}
        source_image = image_metadata.get("image_id") or image_metadata.get("image_uuid")
        attached_servers = {
            item.get("server_id") or item.get("serverId") for item in (volume.attachments or [])
        }
        if volume.size != size or metadata.get("source_image_id") != image.id:
            raise OwnershipError(f"owned volume {volume.name} has incompatible source or size")
        if source_image != image.id:
            raise OwnershipError(f"owned volume {volume.name} has a different source image")
        if expected_server_id is None:
            if volume.status != "available" or attached_servers:
                raise OwnershipError(
                    f"owned volume {volume.name} is unexpectedly attached or unavailable"
                )
        elif volume.status != "in-use" or attached_servers != {expected_server_id}:
            raise OwnershipError(
                f"owned volume {volume.name} is not attached only to the expected server"
            )
        return volume

    def _volume(
        self,
        name: str,
        size: int,
        image: Any,
        expected_server_id: str | None = None,
    ) -> Any:
        volume_name = f"{name}-root"
        existing = _exact(self.conn.block_storage.volumes(name=volume_name), volume_name, "volume")
        if existing:
            return self._verify_volume(existing, size, image, expected_server_id)
        else:
            if expected_server_id is not None:
                raise OwnershipError(f"expected boot volume {volume_name} does not exist")
            existing = self.conn.block_storage.create_volume(
                name=volume_name,
                size=size,
                image_id=image.id,
                metadata={**self.metadata, "source_image_id": image.id},
            )
        existing = self.conn.block_storage.wait_for_status(
            existing, status="available", failures=["error"]
        )
        return self._verify_volume(existing, size, image, None)

    def _node_port(
        self,
        *,
        server_name: str,
        network: Any,
        subnet: Any,
        security_group: Any,
        vip: str | None,
    ) -> Any:
        name = f"{server_name}-port"
        desired_pairs = {vip} if vip else set()
        port = _exact(self.conn.network.ports(name=name), name, "node port")
        if port:
            port = _require_owned(port, self.uid, "node port")
            subnets = {item["subnet_id"] for item in port.fixed_ips}
            pairs = {
                item["ip_address"] if isinstance(item, dict) else item.ip_address
                for item in (port.allowed_address_pairs or [])
            }
            if (
                port.network_id != network.id
                or len(port.fixed_ips) != 1
                or subnets != {subnet.id}
                or set(port.security_group_ids or []) != {security_group.id}
                or port.is_port_security_enabled is not True
                or pairs != desired_pairs
            ):
                raise OwnershipError(f"owned node port {name} has incompatible topology")
            return port
        resource = self.conn.network.create_port(
            name=name,
            network_id=network.id,
            fixed_ips=[{"subnet_id": subnet.id}],
            security_group_ids=[security_group.id],
            port_security_enabled=True,
            allowed_address_pairs=[{"ip_address": vip}] if vip else [],
        )
        return self._set_network_tags(resource)

    def _verify_server(
        self,
        server: Any,
        *,
        role: str,
        flavor: Any,
        keypair: Any,
        volume: Any,
        port: Any,
        security_group: Any,
    ) -> Any:
        server = self.conn.compute.get_server(server.id)
        _require_owned(server, self.uid, "server")
        attached = {
            item.get("id") if isinstance(item, dict) else item.id
            for item in (getattr(server, "attached_volumes", None) or [])
        }
        groups = {
            item.get("name") if isinstance(item, dict) else item.name
            for item in (getattr(server, "security_groups", None) or [])
        }
        metadata = getattr(server, "metadata", None) or {}
        current_port = self.conn.network.get_port(port.id)
        _require_owned(current_port, self.uid, "node port")
        if not _server_uses_flavor(server, flavor):
            raise OwnershipError(f"owned server {server.name} has a different flavor")
        if metadata.get("customer_cluster_role") != role:
            raise OwnershipError(f"owned server {server.name} has a different role")
        if server.key_name != keypair.name:
            raise OwnershipError(f"owned server {server.name} has a different keypair")
        if attached != {volume.id}:
            raise OwnershipError(f"owned server {server.name} has unexpected volume attachments")
        if current_port.device_id != server.id:
            raise OwnershipError(f"owned server {server.name} is not attached to its owned port")
        if set(current_port.security_group_ids or []) != {security_group.id}:
            raise OwnershipError(f"owned server {server.name} has a different security group")
        if groups and groups != {security_group.name}:
            raise OwnershipError(f"owned server {server.name} reports a different security group")
        return server

    def _server(
        self,
        *,
        name: str,
        role: str,
        machine: dict[str, Any],
        network: Any,
        subnet: Any,
        security_group: Any,
        image: Any,
        keypair: Any,
        vip: str | None = None,
    ) -> Any:
        existing = _exact(self.conn.compute.servers(name=name), name, "server")
        if existing:
            existing = _require_owned(existing, self.uid, "server")
        flavor = self._flavor(machine["flavor"])
        volume = self._volume(
            name,
            machine["rootVolumeGB"],
            image,
            expected_server_id=existing.id if existing else None,
        )
        port = self._node_port(
            server_name=name,
            network=network,
            subnet=subnet,
            security_group=security_group,
            vip=vip,
        )
        if not existing:
            metadata = {**self.metadata, "customer_cluster_role": role}
            existing = self.conn.compute.create_server(
                name=name,
                flavor_id=flavor.id,
                networks=[{"port": port.id}],
                key_name=keypair.name,
                metadata=metadata,
                user_data=base64.b64encode(self._cloud_init().encode()).decode(),
                block_device_mapping=[
                    {
                        "uuid": volume.id,
                        "source_type": "volume",
                        "destination_type": "volume",
                        "boot_index": 0,
                        "delete_on_termination": False,
                    }
                ],
            )
        existing = self.conn.compute.wait_for_server(existing, status="ACTIVE", failures=["ERROR"])
        return self._verify_server(
            existing,
            role=role,
            flavor=flavor,
            keypair=keypair,
            volume=volume,
            port=port,
            security_group=security_group,
        )

    def _server_port(self, server: Any, network: Any) -> Any:
        ports = list(self.conn.network.ports(device_id=server.id, network_id=network.id))
        if len(ports) != 1:
            raise RuntimeError(f"server {server.name} does not have exactly one cluster port")
        return ports[0]

    def _floating_ip(self, jump: Any, network: Any, external: Any) -> str:
        port = self._server_port(jump, network)
        description = f"ManagedCluster {self.uid} jumphost"
        matches = list(self.conn.network.ips(port_id=port.id))
        if len(matches) > 1:
            raise OwnershipError("multiple jumphost floating IPs exist")
        floating = (
            matches[0]
            if matches
            else self._set_network_tags(
                self.conn.network.create_ip(
                    floating_network_id=external.id,
                    port_id=port.id,
                    description=description,
                )
            )
        )
        _require_owned(floating, self.uid, "floating IP")
        if floating.description != description:
            raise OwnershipError("owned jumphost floating IP has an unexpected description")
        if floating.floating_network_id != external.id:
            raise OwnershipError("owned jumphost floating IP uses a different floating network")
        return floating.floating_ip_address

    def _vip(self, kind: str, address: str | None, network: Any, subnet: Any) -> Any | None:
        if not address:
            return None
        name = stable_name(self.slug, f"{kind}-vip")
        port = _exact(self.conn.network.ports(name=name), name, "VIP port")
        if port:
            _require_owned(port, self.uid, "VIP port")
            actual = {item["ip_address"] for item in port.fixed_ips}
            subnets = {item["subnet_id"] for item in port.fixed_ips}
            if (
                actual != {address}
                or len(port.fixed_ips) != 1
                or subnets != {subnet.id}
                or port.network_id != network.id
                or port.is_port_security_enabled is not False
                or bool(getattr(port, "security_group_ids", None))
            ):
                raise OwnershipError(f"owned VIP port {name} has incompatible topology")
            return port
        resource = self.conn.network.create_port(
            name=name,
            network_id=network.id,
            fixed_ips=[{"subnet_id": subnet.id, "ip_address": address}],
            port_security_enabled=False,
        )
        return self._set_network_tags(resource)

    def _private_ip(self, server: Any, network: Any) -> str:
        port = self._server_port(server, network)
        addresses = [
            item["ip_address"]
            for item in port.fixed_ips
            if ipaddress.ip_address(item["ip_address"]).version == 4
        ]
        if len(addresses) != 1:
            raise RuntimeError(f"server {server.name} does not have one private IPv4 address")
        return addresses[0]

    def provision(self) -> dict[str, Any]:
        network = self._network()
        subnet = self._subnet(network)
        external = self._external_network()
        self._router(subnet, external)
        cluster_sg, jump_sg = self._security_groups()
        keypair = self._keypair()
        image = self._image()
        self._vip("api", self.data["network"]["apiVipAddress"], network, subnet)
        self._vip("ingress", self.data["network"]["ingressVipAddress"], network, subnet)

        jump = self._server(
            name=stable_name(self.slug, "jumphost"),
            role="jumphost",
            machine=self.data["openstack"]["jumphost"],
            network=network,
            subnet=subnet,
            security_group=jump_sg,
            image=image,
            keypair=keypair,
        )
        jump_fip = self._floating_ip(jump, network, external)
        controllers = []
        workers = []
        for index in range(1, self.data["nodes"]["controllers"] + 1):
            server = self._server(
                name=stable_name(self.slug, f"controller-{index:02d}"),
                role="controller",
                machine=self.data["openstack"]["controller"],
                network=network,
                subnet=subnet,
                security_group=cluster_sg,
                image=image,
                keypair=keypair,
                vip=self.data["network"]["apiVipAddress"],
            )
            controllers.append({"name": server.name, "ip": self._private_ip(server, network)})
        for index in range(1, self.data["nodes"]["workers"] + 1):
            server = self._server(
                name=stable_name(self.slug, f"worker-{index:02d}"),
                role="worker",
                machine=self.data["openstack"]["worker"],
                network=network,
                subnet=subnet,
                security_group=cluster_sg,
                image=image,
                keypair=keypair,
                vip=self.data["network"]["ingressVipAddress"],
            )
            workers.append({"name": server.name, "ip": self._private_ip(server, network)})
        return {
            "jumphost": {"name": jump.name, "floating_ip": jump_fip},
            "controllers": controllers,
            "workers": workers,
            "api_vip": self.data["network"]["apiVipAddress"],
            "ingress_vip": self.data["network"]["ingressVipAddress"],
        }
