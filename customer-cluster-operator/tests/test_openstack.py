import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openstack.block_storage.v3.volume import Volume
from openstack.compute.v2.server import Server
from openstack.exceptions import ConflictException
from openstack.network.v2.port import Port
from openstack.network.v2.security_group_rule import SecurityGroupRule

from customer_cluster_operator.errors import OwnershipError, ValidationError
from customer_cluster_operator.openstack import (
    Provisioner,
    _exact,
    _server_uses_flavor,
    read_public_keys,
    scoped_connection,
    stable_name,
)


def return_tagged(resource, tags):
    resource.tags = tags
    return resource


def test_stable_name_is_bounded():
    assert len(stable_name("a" * 63, "controller-01")) <= 63
    assert stable_name("example", "network") == "cc-example-network"


def test_exact_fails_closed_on_duplicates():
    resources = [SimpleNamespace(name="same"), SimpleNamespace(name="same")]
    with pytest.raises(OwnershipError, match="multiple"):
        _exact(resources, "same", "network")


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (SimpleNamespace(flavor_id="flavor-id", flavor=None), True),
        (Server(flavor={"original_name": "b2.c1r2"}), True),
        (SimpleNamespace(flavor_id=None, flavor={"id": "flavor-id"}), True),
        (Server(flavor={"original_name": "different"}), False),
        (Server(flavor={}), False),
    ],
)
def test_server_flavor_matches_nova_response_formats(server, expected):
    flavor = SimpleNamespace(id="flavor-id", name="b2.c1r2")
    assert _server_uses_flavor(server, flavor) is expected


def test_public_keys_reject_private_material(tmp_path):
    path = tmp_path / "keys"
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    with pytest.raises(ValidationError, match="public keys only"):
        read_public_keys(path)


def test_public_keys_accept_comments_and_multiple_keys(tmp_path):
    path = tmp_path / "keys"
    path.write_text("# admin\nssh-ed25519 QUFBQQ== admin\nssh-rsa QkJCQg==\n")
    assert len(read_public_keys(path)) == 2


def test_existing_unowned_network_is_rejected(provisioning_input):
    network_api = Mock()
    network_api.networks.return_value = [SimpleNamespace(name="cc-example-network", tags=[])]
    connection = SimpleNamespace(network=network_api)
    provisioner = Provisioner(connection, provisioning_input.data, ["ssh-ed25519 QUFBQQ=="])
    with pytest.raises(OwnershipError, match="belongs to another cluster UID"):
        provisioner._network()


def test_owned_network_is_reused(provisioning_input):
    tag = "customer-cluster-uid=12345678-1234-1234-1234-123456789abc"
    existing = SimpleNamespace(name="cc-example-network", tags=[tag])
    network_api = Mock()
    network_api.networks.return_value = [existing]
    connection = SimpleNamespace(network=network_api)
    provisioner = Provisioner(connection, provisioning_input.data, ["ssh-ed25519 QUFBQQ=="])
    assert provisioner._network() is existing
    network_api.create_network.assert_not_called()


def test_network_creation_sets_tags_after_create(provisioning_input):
    network_api = Mock()
    network_api.networks.return_value = []
    created = SimpleNamespace(id="network")
    network_api.create_network.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )

    assert provisioner._network() is created
    network_api.create_network.assert_called_once_with(name="cc-example-network")
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_cloud_init_disables_passwords(provisioning_input):
    provisioner = Provisioner(Mock(), provisioning_input.data, ["ssh-ed25519 QUFBQQ=="])
    cloud_init = provisioner._cloud_init()
    assert "ssh_pwauth: false" in cloud_init
    assert "lock_passwd: true" in cloud_init
    assert "name: root" in cloud_init


def test_volume_is_owned_and_idempotent(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    image = SimpleNamespace(id="image")
    volume = Volume(
        name="cc-example-controller-01-root",
        size=80,
        status="in-use",
        metadata={"customer_cluster_uid": uid, "source_image_id": "image"},
        volume_image_metadata={"image_id": "image"},
        attachments=[{"server_id": "server"}],
    )
    storage = Mock()
    storage.volumes.return_value = [volume]
    storage.wait_for_status.return_value = volume
    provisioner = Provisioner(
        SimpleNamespace(block_storage=storage),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    assert (
        provisioner._volume("cc-example-controller-01", 80, image, expected_server_id="server")
        is volume
    )
    storage.create_volume.assert_not_called()


def test_vip_retry_rejects_changed_address(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    port = Port(
        name="cc-example-api-vip",
        tags=[f"customer-cluster-uid={uid}"],
        fixed_ips=[{"ip_address": "10.44.0.99", "subnet_id": "subnet"}],
        network_id="network",
        is_port_security_enabled=False,
    )
    network_api = Mock()
    network_api.ports.return_value = [port]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="incompatible topology"):
        provisioner._vip(
            "api",
            "10.44.0.10",
            SimpleNamespace(id="network"),
            SimpleNamespace(id="subnet"),
        )


def test_connection_auth_plugin_is_constructed_with_project_scope(
    monkeypatch, provisioning_input, tmp_path
):
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        """clouds:
  production:
    auth_type: v3password
    auth:
      auth_url: https://identity.example/v3
      username: admin
      password: test-password
      user_domain_name: Default
      system_scope: all
"""
    )
    connection = Mock()
    connection.current_project_id = "project-id"
    connection.identity.get_project.return_value = SimpleNamespace(name="customer-example")
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(
        "customer_cluster_operator.openstack.openstack.connection.Connection", constructor
    )
    assert scoped_connection(provisioning_input.data, str(clouds)) is connection
    cloud = constructor.call_args.kwargs["config"]
    plugin = cloud.get_auth()
    assert plugin.project_id == "project-id"
    assert plugin.project_name is None
    assert plugin.system_scope is None
    connection.authorize.assert_called_once()


def test_connection_rejects_wrong_scope(monkeypatch, provisioning_input):
    loader = Mock()
    loader.cloud_config = {"clouds": {"production": {"auth": {"username": "admin"}}}}
    loader.get_one_cloud.return_value = SimpleNamespace()
    connection = Mock(current_project_id="other-project")
    monkeypatch.setattr(
        "customer_cluster_operator.openstack.OpenStackConfig", Mock(return_value=loader)
    )
    monkeypatch.setattr(
        "customer_cluster_operator.openstack.openstack.connection.Connection",
        Mock(return_value=connection),
    )
    with pytest.raises(ValidationError, match="could not be scoped"):
        scoped_connection(provisioning_input.data, "/clouds.yaml")


def test_subnet_creation_uses_profile_network(provisioning_input):
    network_api = Mock()
    network_api.subnets.return_value = []
    created = SimpleNamespace(id="subnet")
    network_api.create_subnet.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    assert provisioner._subnet(SimpleNamespace(id="network")) is created
    kwargs = network_api.create_subnet.call_args.kwargs
    assert kwargs["cidr"] == "10.44.0.0/24"
    assert kwargs["dns_nameservers"] == ["1.1.1.1", "9.9.9.9"]
    assert "tags" not in kwargs
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_router_rejects_wrong_external_network(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    router = SimpleNamespace(
        id="router",
        name="cc-example-router",
        tags=[f"customer-cluster-uid={uid}"],
        external_gateway_info={"network_id": "wrong"},
    )
    network_api = Mock()
    network_api.routers.return_value = [router]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="different external"):
        provisioner._router(SimpleNamespace(id="subnet"), SimpleNamespace(id="public"))


def test_router_rejects_additional_internal_interfaces(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    router = SimpleNamespace(
        id="router",
        name="cc-example-router",
        tags=[f"customer-cluster-uid={uid}"],
        external_gateway_info={"network_id": "public", "enable_snat": True},
    )
    interfaces = [
        Port(
            device_owner="network:router_interface",
            fixed_ips=[{"subnet_id": "subnet"}],
        ),
        Port(
            device_owner="network:router_interface",
            fixed_ips=[{"subnet_id": "unexpected-subnet"}],
        ),
    ]
    network_api = Mock()
    network_api.routers.return_value = [router]
    network_api.ports.return_value = interfaces
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="unexpected internal"):
        provisioner._router(SimpleNamespace(id="subnet"), SimpleNamespace(id="public"))


def test_router_creation_sets_tags_before_interface(provisioning_input):
    network_api = Mock()
    network_api.routers.return_value = []
    created = SimpleNamespace(id="router")
    network_api.create_router.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )

    assert (
        provisioner._router(SimpleNamespace(id="subnet"), SimpleNamespace(id="public")) is created
    )
    request = network_api.create_router.call_args.kwargs
    assert request["external_gateway_info"] == {"network_id": "public"}
    assert "tags" not in request
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)
    network_api.add_interface_to_router.assert_called_once_with(created, subnet_id="subnet")


@pytest.mark.parametrize("interfaces", [[], ["subnet"]])
def test_router_partial_retry_accepts_zero_or_expected_interface(provisioning_input, interfaces):
    uid = provisioning_input.data["cluster"]["uid"]
    router = SimpleNamespace(
        id="router",
        name="cc-example-router",
        tags=[f"customer-cluster-uid={uid}"],
        external_gateway_info={"network_id": "public", "enable_snat": True},
    )
    ports = [
        Port(
            device_owner="network:router_interface",
            fixed_ips=[{"subnet_id": subnet_id}],
        )
        for subnet_id in interfaces
    ]
    network_api = Mock()
    network_api.routers.return_value = [router]
    network_api.ports.return_value = ports
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    assert provisioner._router(SimpleNamespace(id="subnet"), SimpleNamespace(id="public")) is router
    if interfaces:
        network_api.add_interface_to_router.assert_not_called()
    else:
        network_api.add_interface_to_router.assert_called_once_with(router, subnet_id="subnet")


def test_volume_rejects_source_or_attachment_drift(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    volume = Volume(
        name="cc-example-controller-01-root",
        size=80,
        status="in-use",
        metadata={"customer_cluster_uid": uid, "source_image_id": "wrong-image"},
        volume_image_metadata={"image_id": "image"},
        attachments=[{"server_id": "server"}],
    )
    provisioner = Provisioner(Mock(), provisioning_input.data, ["ssh-ed25519 QUFBQQ=="])
    with pytest.raises(OwnershipError, match="incompatible source"):
        provisioner._verify_volume(volume, 80, SimpleNamespace(id="image"), "server")
    volume.metadata["source_image_id"] = "image"
    volume.attachments = [{"server_id": "another-server"}]
    with pytest.raises(OwnershipError, match="expected server"):
        provisioner._verify_volume(volume, 80, SimpleNamespace(id="image"), "server")


def test_new_volume_records_and_verifies_source_image(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    image = SimpleNamespace(id="image")
    volume = Volume(
        id="volume",
        name="cc-example-controller-01-root",
        size=80,
        status="available",
        metadata={
            "customer_cluster_uid": uid,
            "customer_cluster_slug": "example",
            "source_image_id": "image",
        },
        volume_image_metadata={"image_id": "image"},
        attachments=[],
    )
    storage = Mock()
    storage.volumes.return_value = []
    storage.create_volume.return_value = volume
    storage.wait_for_status.return_value = volume
    provisioner = Provisioner(
        SimpleNamespace(block_storage=storage),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    assert provisioner._volume("cc-example-controller-01", 80, image) is volume
    request = storage.create_volume.call_args.kwargs
    assert request["image_id"] == "image"
    assert request["metadata"]["source_image_id"] == "image"


def test_retained_conflict_explains_manual_recovery(provisioning_input):
    network_api = Mock()
    network_api.networks.return_value = [SimpleNamespace(name="cc-example-network", tags=[])]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="automatic adoption is disabled") as error:
        provisioner._network()
    assert "Restore the original ManagedCluster UID" in str(error.value)


def test_security_rule_is_idempotent(provisioning_input):
    rule = SecurityGroupRule(
        direction="ingress",
        ether_type="IPv4",
        protocol="tcp",
        port_range_min=22,
        port_range_max=22,
        remote_ip_prefix="192.0.2.1/32",
        remote_group_id=None,
    )
    network_api = Mock()
    network_api.security_group_rules.return_value = [rule]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    provisioner._rule(
        SimpleNamespace(id="sg"),
        direction="ingress",
        ether_type="IPv4",
        protocol="tcp",
        port_range_min=22,
        port_range_max=22,
        remote_ip_prefix="192.0.2.1/32",
    )
    network_api.create_security_group_rule.assert_not_called()


def test_security_rule_duplicate_conflict_is_idempotent(provisioning_input):
    network_api = Mock()
    network_api.security_group_rules.return_value = []
    network_api.create_security_group_rule.side_effect = ConflictException(
        "Security group rule already exists"
    )
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )

    provisioner._rule(
        SimpleNamespace(id="sg"),
        direction="ingress",
        ether_type="IPv4",
        remote_group_id="remote-sg",
    )

    network_api.create_security_group_rule.assert_called_once_with(
        security_group_id="sg",
        direction="ingress",
        ether_type="IPv4",
        remote_group_id="remote-sg",
    )


def test_security_group_creation_sets_tags_after_create(provisioning_input):
    network_api = Mock()
    network_api.security_groups.return_value = []
    created = SimpleNamespace(id="security-group")
    network_api.create_security_group.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )

    assert provisioner._security_group("cluster-sg") is created
    assert "tags" not in network_api.create_security_group.call_args.kwargs
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_server_boots_from_owned_volume(provisioning_input, monkeypatch):
    compute = Mock()
    compute.servers.return_value = []
    compute.flavors.return_value = [SimpleNamespace(name="b2.c4r8", id="flavor")]
    created = SimpleNamespace(name="controller", id="server")
    compute.create_server.return_value = created
    compute.wait_for_server.return_value = created
    provisioner = Provisioner(
        SimpleNamespace(compute=compute),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    monkeypatch.setattr(provisioner, "_volume", Mock(return_value=SimpleNamespace(id="volume")))
    monkeypatch.setattr(provisioner, "_node_port", Mock(return_value=SimpleNamespace(id="port")))
    monkeypatch.setattr(provisioner, "_verify_server", Mock(return_value=created))
    result = provisioner._server(
        name="controller",
        role="controller",
        machine={"flavor": "b2.c4r8", "rootVolumeGB": 80},
        network=SimpleNamespace(id="network"),
        subnet=SimpleNamespace(id="subnet"),
        security_groups=[SimpleNamespace(name="cluster-sg")],
        image=SimpleNamespace(id="image"),
        keypair=SimpleNamespace(name="key"),
    )
    assert result is created
    request = compute.create_server.call_args.kwargs
    mapping = request["block_device_mapping"][0]
    assert mapping["uuid"] == "volume"
    assert mapping["source_type"] == "volume"
    assert mapping["delete_on_termination"] is False
    assert request["networks"] == [{"port": "port"}]
    assert "security_groups" not in request
    assert base64.b64decode(request["user_data"]).startswith(b"#cloud-config")
    sdk_body = (
        Server(
            block_device_mapping=request["block_device_mapping"],
            user_data=request["user_data"],
        )
        ._prepare_request(requires_id=False)
        .body["server"]
    )
    assert "block_device_mapping_v2" in sdk_body
    assert "block_device_mapping" not in sdk_body


def test_floating_ip_rejects_unowned_address(provisioning_input, monkeypatch):
    network_api = Mock()
    network_api.ips.return_value = [
        SimpleNamespace(id="fip", name=None, tags=[], description="foreign")
    ]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="belongs to another cluster UID"):
        provisioner._floating_ip(
            "jumphost",
            SimpleNamespace(
                id="port", fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.20"}]
            ),
            SimpleNamespace(id="public"),
        )


def test_floating_ip_validates_floating_network(provisioning_input, monkeypatch):
    uid = provisioning_input.data["cluster"]["uid"]
    floating = SimpleNamespace(
        id="fip",
        name=None,
        tags=[f"customer-cluster-uid={uid}"],
        description=f"ManagedCluster {uid} jumphost",
        floating_network_id="other-public",
        floating_ip_address="192.0.2.10",
        port_id="port",
        fixed_ip_address="10.44.0.20",
    )
    network_api = Mock()
    network_api.ips.return_value = [floating]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="different floating network"):
        provisioner._floating_ip(
            "jumphost",
            SimpleNamespace(
                id="port", fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.20"}]
            ),
            SimpleNamespace(id="public"),
        )


def test_floating_ip_creation_sets_tags_after_create(provisioning_input, monkeypatch):
    network_api = Mock()
    network_api.ips.return_value = []
    created = SimpleNamespace(
        id="floating-ip",
        description=("ManagedCluster 12345678-1234-1234-1234-123456789abc jumphost"),
        floating_network_id="public",
        floating_ip_address="192.0.2.10",
        port_id="port",
        fixed_ip_address="10.44.0.20",
    )
    network_api.create_ip.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    assert (
        provisioner._floating_ip(
            "jumphost",
            SimpleNamespace(
                id="port", fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.20"}]
            ),
            SimpleNamespace(id="public"),
        )
        == "192.0.2.10"
    )
    assert "tags" not in network_api.create_ip.call_args.kwargs
    assert network_api.create_ip.call_args.kwargs["port_id"] == "port"
    assert network_api.create_ip.call_args.kwargs["fixed_ip_address"] == "10.44.0.20"
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_node_port_has_role_vip_and_is_idempotent(provisioning_input):
    network_api = Mock()
    created = SimpleNamespace(id="port")
    network_api.ports.return_value = []
    network_api.create_port.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    result = provisioner._node_port(
        server_name="cc-example-controller-01",
        network=SimpleNamespace(id="network"),
        subnet=SimpleNamespace(id="subnet"),
        security_groups=[
            SimpleNamespace(id="cluster-sg"),
            SimpleNamespace(id="api-sg"),
        ],
        vip="10.44.0.10",
    )
    assert result is created
    kwargs = network_api.create_port.call_args.kwargs
    assert kwargs["allowed_address_pairs"] == [{"ip_address": "10.44.0.10"}]
    assert kwargs["security_group_ids"] == ["api-sg", "cluster-sg"]
    assert kwargs["fixed_ips"] == [{"subnet_id": "subnet"}]
    assert "tags" not in kwargs
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_adopted_node_port_topology_must_match(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    port = Port(
        name="cc-example-worker-01-port",
        tags=[f"customer-cluster-uid={uid}"],
        network_id="wrong",
        fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.20"}],
        security_group_ids=["cluster-sg"],
        is_port_security_enabled=True,
        allowed_address_pairs=[{"ip_address": "10.44.0.11"}],
    )
    network_api = Mock()
    network_api.ports.return_value = [port]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="incompatible topology"):
        provisioner._node_port(
            server_name="cc-example-worker-01",
            network=SimpleNamespace(id="network"),
            subnet=SimpleNamespace(id="subnet"),
            security_groups=[SimpleNamespace(id="cluster-sg")],
            vip="10.44.0.11",
        )


def test_adopted_server_role_and_attachments_are_verified(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    server = SimpleNamespace(
        id="server",
        name="controller",
        metadata={"customer_cluster_uid": uid, "customer_cluster_role": "worker"},
        flavor_id="flavor",
        flavor=None,
        key_name="key",
        attached_volumes=[{"id": "volume"}],
        security_groups=[{"name": "cluster-sg"}],
    )
    port = Port(
        id="port",
        name="port",
        metadata={"customer_cluster_uid": uid},
        tags=[],
        device_id="server",
        security_group_ids=["sg"],
    )
    compute = Mock()
    compute.get_server.return_value = server
    network = Mock()
    network.get_port.return_value = port
    provisioner = Provisioner(
        SimpleNamespace(compute=compute, network=network),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="different role"):
        provisioner._verify_server(
            server,
            role="controller",
            flavor=SimpleNamespace(id="flavor"),
            keypair=SimpleNamespace(name="key"),
            volume=SimpleNamespace(id="volume"),
            port=port,
            security_groups=[SimpleNamespace(id="sg", name="cluster-sg")],
        )


def test_vip_validates_network_subnet_and_port_security(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    port = SimpleNamespace(
        name="cc-example-api-vip",
        tags=[f"customer-cluster-uid={uid}"],
        network_id="network",
        fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.10"}],
        is_port_security_enabled=True,
    )
    network_api = Mock()
    network_api.ports.return_value = [port]
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="incompatible topology"):
        provisioner._vip(
            "api",
            "10.44.0.10",
            SimpleNamespace(id="network"),
            SimpleNamespace(id="subnet"),
        )


def test_vip_creation_sets_tags_after_create(provisioning_input):
    network_api = Mock()
    network_api.ports.return_value = []
    created = SimpleNamespace(id="vip-port")
    network_api.create_port.return_value = created
    network_api.set_tags.side_effect = return_tagged
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )

    assert (
        provisioner._vip(
            "api",
            "10.44.0.10",
            SimpleNamespace(id="network"),
            SimpleNamespace(id="subnet"),
        )
        is created
    )
    request = network_api.create_port.call_args.kwargs
    assert request["fixed_ips"] == [{"subnet_id": "subnet", "ip_address": "10.44.0.10"}]
    assert "tags" not in request
    network_api.set_tags.assert_called_once_with(created, provisioner.tags)


def test_provision_routes_role_vips_to_explicit_node_ports(provisioning_input, monkeypatch):
    provisioner = Provisioner(Mock(), provisioning_input.data, ["ssh-ed25519 QUFBQQ=="])
    network = SimpleNamespace(id="network")
    subnet = SimpleNamespace(id="subnet")
    external = SimpleNamespace(id="public")
    cluster_sg = SimpleNamespace(id="cluster-sg", name="cluster-sg")
    jump_sg = SimpleNamespace(id="jump-sg", name="jump-sg")
    api_sg = SimpleNamespace(id="api-sg", name="api-sg")
    ingress_sg = SimpleNamespace(id="ingress-sg", name="ingress-sg")
    keypair = SimpleNamespace(name="key")
    image = SimpleNamespace(id="image")
    monkeypatch.setattr(provisioner, "_network", lambda: network)
    monkeypatch.setattr(provisioner, "_subnet", lambda value: subnet)
    monkeypatch.setattr(provisioner, "_external_network", lambda: external)
    monkeypatch.setattr(provisioner, "_router", Mock())
    monkeypatch.setattr(
        provisioner,
        "_security_groups",
        lambda: (cluster_sg, jump_sg, api_sg, ingress_sg),
    )
    monkeypatch.setattr(provisioner, "_keypair", lambda: keypair)
    monkeypatch.setattr(provisioner, "_image", lambda: image)
    monkeypatch.setattr(
        provisioner,
        "_vip",
        lambda kind, address, network, subnet: SimpleNamespace(
            id=f"{kind}-vip", fixed_ips=[{"ip_address": address}]
        ),
    )
    calls = []

    def server(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(name=kwargs["name"], id=kwargs["name"])

    monkeypatch.setattr(provisioner, "_server", server)
    monkeypatch.setattr(provisioner, "_floating_ip", lambda *args: "192.0.2.10")
    monkeypatch.setattr(
        provisioner,
        "_server_port",
        lambda server, network: SimpleNamespace(
            id=f"{server.id}-port", fixed_ips=[{"ip_address": "10.44.0.20"}]
        ),
    )
    monkeypatch.setattr(provisioner, "_private_ip", lambda server, value: "10.44.0.20")
    provisioner.provision()
    controllers = [item for item in calls if item["role"] == "controller"]
    workers = [item for item in calls if item["role"] == "worker"]
    jump = [item for item in calls if item["role"] == "jumphost"]
    assert len(controllers) == 3
    assert len(workers) == 6
    assert all(item["vip"] == "10.44.0.10" for item in controllers)
    assert all(item["vip"] == "10.44.0.11" for item in workers)
    assert all(
        {group.id for group in item["security_groups"]} == {"cluster-sg", "api-sg"}
        for item in controllers
    )
    assert all(
        {group.id for group in item["security_groups"]} == {"cluster-sg", "ingress-sg"}
        for item in workers
    )
    assert "vip" not in jump[0]


def test_owned_node_port_migrates_sunet_two_to_exact_role_groups(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    port = Port(
        id="port",
        name="cc-example-controller-01-port",
        tags=[f"customer-cluster-uid={uid}"],
        network_id="network",
        fixed_ips=[{"subnet_id": "subnet", "ip_address": "10.44.0.20"}],
        security_group_ids=["cluster-sg", "sunet-two"],
        is_port_security_enabled=True,
        allowed_address_pairs=[{"ip_address": "10.44.0.10"}],
    )
    updated = Port(**port.to_dict())
    updated.security_group_ids = ["api-sg", "cluster-sg"]
    network_api = Mock()
    network_api.ports.return_value = [port]
    network_api.update_port.return_value = updated
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    result = provisioner._node_port(
        server_name=port.name.removesuffix("-port"),
        network=SimpleNamespace(id="network"),
        subnet=SimpleNamespace(id="subnet"),
        security_groups=[SimpleNamespace(id="cluster-sg"), SimpleNamespace(id="api-sg")],
        vip="10.44.0.10",
    )
    assert result.security_group_ids == ["api-sg", "cluster-sg"]
    network_api.update_port.assert_called_once_with(
        port, security_group_ids=["api-sg", "cluster-sg"]
    )


def test_endpoint_floating_ip_rejects_duplicate_associations(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    description = f"ManagedCluster {uid} api"
    floating_ips = [SimpleNamespace(id=value, description=description) for value in ("one", "two")]
    network_api = Mock()
    network_api.ips.return_value = floating_ips
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="multiple api"):
        provisioner._floating_ip(
            "api",
            SimpleNamespace(id="port", fixed_ips=[{"ip_address": "10.44.0.10"}]),
            SimpleNamespace(id="public"),
        )


def test_endpoint_floating_ip_reuses_detached_owned_address(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    network_api = Mock()
    floating = SimpleNamespace(
        id="fip",
        tags=[],
        description=f"ManagedCluster {uid} api",
        floating_network_id="public",
        floating_ip_address="192.0.2.11",
        port_id=None,
        fixed_ip_address=None,
    )
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    floating.tags = provisioner.tags
    network_api.ips.side_effect = [[], [floating]]
    attached = SimpleNamespace(**vars(floating))
    attached.port_id = "api-port"
    attached.fixed_ip_address = "10.44.0.10"
    network_api.update_ip.return_value = attached
    assert (
        provisioner._floating_ip(
            "api",
            SimpleNamespace(id="api-port", fixed_ips=[{"ip_address": "10.44.0.10"}]),
            SimpleNamespace(id="public"),
        )
        == "192.0.2.11"
    )
    network_api.update_ip.assert_called_once_with(
        floating, port_id="api-port", fixed_ip_address="10.44.0.10"
    )


def test_endpoint_security_groups_have_only_exact_public_rules(provisioning_input):
    rules = {}
    network_api = Mock()
    uid = provisioning_input.data["cluster"]["uid"]

    def security_groups(name):
        return [SimpleNamespace(id=name, name=name, tags=[f"customer-cluster-uid={uid}"])]

    def security_group_rules(*, security_group_id):
        return rules.setdefault(security_group_id, [])

    def create_security_group_rule(*, security_group_id, **values):
        rule = SecurityGroupRule(**values)
        rules.setdefault(security_group_id, []).append(rule)
        return rule

    network_api.security_groups.side_effect = security_groups
    network_api.security_group_rules.side_effect = security_group_rules
    network_api.create_security_group_rule.side_effect = create_security_group_rule
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    cluster, jump, api, ingress = provisioner._security_groups()
    assert {rule.port_range_min for rule in rules[api.id]} == {6443}
    assert {rule.remote_ip_prefix for rule in rules[api.id]} == {"0.0.0.0/0"}
    assert {rule.port_range_min for rule in rules[ingress.id]} == {80, 443}
    assert {rule.remote_ip_prefix for rule in rules[ingress.id]} == {"0.0.0.0/0"}
    assert cluster.id.endswith("cluster-sg")
    assert jump.id.endswith("jumphost-sg")


def test_endpoint_security_group_rejects_nonpublic_source(provisioning_input):
    uid = provisioning_input.data["cluster"]["uid"]
    groups = {
        suffix: SimpleNamespace(
            id=f"cc-example-{suffix}",
            name=f"cc-example-{suffix}",
            tags=[f"customer-cluster-uid={uid}"],
        )
        for suffix in ("cluster-sg", "jumphost-sg", "api-sg", "ingress-sg")
    }
    rules = {
        groups["api-sg"].id: [
            SecurityGroupRule(
                direction="ingress",
                ether_type="IPv4",
                protocol="tcp",
                port_range_min=6443,
                port_range_max=6443,
                remote_ip_prefix="10.0.0.0/8",
            )
        ]
    }
    network_api = Mock()
    network_api.security_groups.side_effect = lambda name: [
        next(group for group in groups.values() if group.name == name)
    ]
    network_api.security_group_rules.side_effect = lambda *, security_group_id: rules.setdefault(
        security_group_id, []
    )

    def create_rule(*, security_group_id, **values):
        rules.setdefault(security_group_id, []).append(SecurityGroupRule(**values))

    network_api.create_security_group_rule.side_effect = create_rule
    provisioner = Provisioner(
        SimpleNamespace(network=network_api),
        provisioning_input.data,
        ["ssh-ed25519 QUFBQQ=="],
    )
    with pytest.raises(OwnershipError, match="unexpected IPv4 ingress"):
        provisioner._security_groups()
