import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from openstack.block_storage.v3.volume import Volume
from openstack.compute.v2.server import Server
from openstack.network.v2.port import Port

from customer_cluster_operator import worker
from customer_cluster_operator.errors import InventoryConflict, ValidationError
from customer_cluster_operator.kube import job_result
from customer_cluster_operator.models import ProvisioningInput
from customer_cluster_operator.openstack import Provisioner, stable_name


@pytest.mark.parametrize("schema", [1, 3, "2", 2.0, True, None])
def test_load_input_rejects_unknown_schema(tmp_path, schema):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"schemaVersion": schema}))
    with pytest.raises(ValidationError, match="unsupported"):
        worker.load_input(path)


def test_load_input_accepts_complete_schema2(tmp_path, provisioning_input):
    path = tmp_path / "input.json"
    path.write_text(provisioning_input.canonical_json)
    assert worker.load_input(path) == provisioning_input.data


@pytest.fixture
def resources():
    return {
        "jumphost": {"name": "jump", "floating_ip": "192.0.2.10"},
        "controllers": [
            {"name": f"controller-{index}", "ip": f"10.44.0.{20 + index}"}
            for index in range(1, 4)
        ],
        "workers": [
            {"name": f"worker-{index}", "ip": f"10.44.0.{30 + index}"}
            for index in range(1, 7)
        ],
        "api_vip": "10.44.0.10",
        "ingress_vip": "10.44.0.11",
        "api_floating_ip": "192.0.2.11",
        "ingress_floating_ip": "192.0.2.12",
    }


@pytest.fixture
def fake_openstack(monkeypatch, resources):
    calls = Mock()
    calls.read_public_keys.return_value = ["ssh-ed25519 QUFBQQ=="]
    calls.scoped_connection.return_value = "connection"
    calls.Provisioner.return_value.provision.return_value = resources
    for name in ("read_public_keys", "scoped_connection", "Provisioner"):
        monkeypatch.setattr(worker, name, getattr(calls, name))
    return calls


def test_worker_provisions_and_only_publishes_inventory(
    monkeypatch, provisioning_input, fake_openstack,
):
    published = {}

    def publish(config, slug, inventory, token, *, cluster_policy: str, provisioning_data: dict):
        published.update(
            config=config, slug=slug, inventory=inventory, token=token,
            cluster_policy=cluster_policy, provisioning_data=provisioning_data,
        )
        return "clusters/example/generated/ansible/hosts.yml", "a" * 40

    monkeypatch.setattr(worker, "publish_inventory", publish)
    result = worker.run(provisioning_input.data, token="secret", clouds_file="/clouds")
    assert result["controllers"] == 3
    assert result["workers"] == 6
    assert result["inventoryPath"].endswith("hosts.yml")
    assert result["inventoryCommit"] == "a" * 40
    assert result["apiFloatingIp"] == "192.0.2.11"
    assert result["ingressFloatingIp"] == "192.0.2.12"
    assert result["schemaVersion"] == 2
    assert result["inputHash"] == provisioning_input.input_hash
    assert result["inventoryInputHash"] == provisioning_input.inventory_hash
    assert result["publicationHash"] == provisioning_input.publication_hash
    assert result["policyInventoryPath"] == "inventory/clusters/example.yml"
    assert "secret" not in json.dumps(result)
    assert published["token"] == "secret"
    assert published["config"] == provisioning_input.data["git"]
    assert published["slug"] == "example"
    assert published["provisioning_data"] == provisioning_input.data
    assert "ProxyJump" in published["inventory"]
    assert yaml.safe_load(published["cluster_policy"]) == {
        "all": {"vars": {
            "customer_cluster_name": "example",
            "customer_cluster_profile": "standard-v1",
            "customer_cluster_node_interface": "ens3",
            "customer_cluster_api_hostname": "api.example.example.org",
            "customer_cluster_argocd_hostname": "argocd.example.example.org",
            "ansible_python_interpreter": "/usr/bin/python3",
        }},
    }
    fake_openstack.scoped_connection.assert_called_once_with(provisioning_input.data, "/clouds")
    fake_openstack.Provisioner.assert_called_once_with(
        "connection", provisioning_input.data, ["ssh-ed25519 QUFBQQ=="]
    )
    fake_openstack.Provisioner.return_value.provision.assert_called_once_with()

    pod = SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid="job-uid")]),
        status=SimpleNamespace(container_statuses=[SimpleNamespace(
            name="provision",
            state=SimpleNamespace(terminated=SimpleNamespace(
                exit_code=0, message=json.dumps(result),
            )),
        )]),
    )
    core = Mock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    parsed = job_result(core, "openstack-operator", job)
    assert parsed == {key: result[key] for key in (
        "inventoryPath", "policyInventoryPath", "inventoryCommit", "inputHash",
        "inventoryInputHash", "publicationHash", "apiFloatingIp", "ingressFloatingIp",
    )}


@pytest.mark.parametrize("entry", ["load", "run"])
@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda data: data.pop("inventory"), id="missing-inventory"),
        pytest.param(lambda data: data.update(inventory=None), id="null-inventory"),
        pytest.param(lambda data: data["inventory"].pop("profileName"), id="missing-profile"),
        pytest.param(lambda data: data["inventory"].pop("apiHostname"), id="missing-api"),
        pytest.param(lambda data: data["inventory"].pop("argocdHostname"), id="missing-argocd"),
        pytest.param(lambda data: data["inventory"].pop("nodeInterface"), id="missing-interface"),
        pytest.param(
            lambda data: data["inventory"].pop("pythonInterpreter"), id="missing-python",
        ),
        pytest.param(
            lambda data: data["inventory"].update(apiHostname="{{ malicious }}"), id="api-template",
        ),
        pytest.param(
            lambda data: data["inventory"].update(argocdHostname="192.0.2.1"), id="argocd-ip",
        ),
        pytest.param(
            lambda data: data["inventory"].update(nodeInterface="ens3;id"), id="interface-command",
        ),
        pytest.param(
            lambda data: data["inventory"].update(pythonInterpreter="/bin/sh"), id="python-shell",
        ),
        pytest.param(
            lambda data: data["inventory"].update(profileName="../foreign"), id="profile-path",
        ),
        pytest.param(
            lambda data: data["inventory"].update(ansible_password="unreviewed"), id="extra-field",
        ),
        pytest.param(lambda data: data.pop("cluster"), id="missing-identity"),
        pytest.param(lambda data: data["cluster"].update(slug="../foreign"), id="slug-path"),
        pytest.param(lambda data: data["cluster"].update(uid="bad\nuid"), id="invalid-uid"),
    ],
)
def test_worker_validates_policy_before_any_openstack_calls(
    monkeypatch, tmp_path, provisioning_input, entry, mutation,
):
    data = deepcopy(provisioning_input.data)
    mutation(data)
    external = {}
    for name in ("read_public_keys", "scoped_connection", "Provisioner", "publish_inventory"):
        external[name] = Mock(side_effect=AssertionError(f"unexpected {name} before validation"))
        monkeypatch.setattr(worker, name, external[name])

    with pytest.raises(ValidationError):
        if entry == "load":
            path = tmp_path / "input.json"
            path.write_text(json.dumps(data))
            worker.load_input(path)
        else:
            worker.run(data, token="secret", clouds_file="/clouds")

    for call in external.values():
        call.assert_not_called()


@pytest.mark.parametrize("schema", [1, 3, "2", 2.0, True, None])
def test_worker_run_rejects_unsupported_schema_before_openstack(
    monkeypatch, provisioning_input, fake_openstack, schema,
):
    data = deepcopy(provisioning_input.data)
    data["schemaVersion"] = schema
    publish = Mock(side_effect=AssertionError("publication before validation"))
    monkeypatch.setattr(worker, "publish_inventory", publish)
    with pytest.raises(ValidationError, match="unsupported"):
        worker.run(data, token="secret")
    assert fake_openstack.mock_calls == []
    publish.assert_not_called()


def test_inventory_conflict_emits_only_safe_failure_receipt(
    monkeypatch, tmp_path, provisioning_input, fake_openstack, capsys,
):
    path = tmp_path / "input.json"
    path.write_text(provisioning_input.canonical_json)
    termination_path = tmp_path / "termination-log"
    monkeypatch.setenv("INPUT_FILE", str(path))
    monkeypatch.setenv("GIT_TOKEN", "token-that-must-not-leak")
    monkeypatch.setenv("OS_CLIENT_CONFIG_FILE", "/clouds")
    publish = Mock(side_effect=InventoryConflict(
        "private YAML: ansible_password=do-not-leak; token-that-must-not-leak"
    ))
    monkeypatch.setattr(worker, "publish_inventory", publish)
    write_result = worker.write_termination_result
    monkeypatch.setattr(worker, "write_termination_result", lambda result: write_result(
        result, termination_path,
    ))

    with pytest.raises(SystemExit) as error:
        worker.main()

    assert error.value.code == 1
    assert json.loads(termination_path.read_text()) == {
        "errorCode": "InventoryConflict",
        "message": "Inventory publication conflicts with existing policy or current desired state",
    }
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip() == (
        "Inventory publication conflicts with existing policy or current desired state"
    )
    assert "do-not-leak" not in termination_path.read_text() + output.err
    assert "token-that-must-not-leak" not in termination_path.read_text() + output.err
    publish.assert_called_once()
    fake_openstack.Provisioner.return_value.provision.assert_called_once_with()


@pytest.mark.parametrize(
    "profile_revision",
    [{"uid": "profile-uid", "generation": 2}, {"uid": "new-uid", "generation": 1}],
)
def test_profile_revision_changes_worker_receipt_but_not_resolved_inventory(
    monkeypatch, provisioning_input, fake_openstack, profile_revision,
):
    publish = Mock(return_value=("clusters/example/generated/ansible/hosts.yml", "a" * 40))
    monkeypatch.setattr(worker, "publish_inventory", publish)
    previous = worker.run(provisioning_input.data, token="secret", clouds_file="/clouds")
    changed_data = deepcopy(provisioning_input.data)
    changed_data["profileRevision"] = profile_revision
    current = worker.run(changed_data, token="secret", clouds_file="/clouds")

    assert current["inputHash"] == previous["inputHash"] == provisioning_input.input_hash
    assert current["inventoryInputHash"] == previous["inventoryInputHash"]
    assert current["publicationHash"] != previous["publicationHash"]
    assert current["publicationHash"] == ProvisioningInput(changed_data).publication_hash
    first_call, second_call = publish.call_args_list
    assert first_call.args == second_call.args
    assert first_call.kwargs["cluster_policy"] == second_call.kwargs["cluster_policy"]
    assert second_call.kwargs["provisioning_data"] == (
        first_call.kwargs["provisioning_data"] | {"profileRevision": profile_revision}
    )
    assert fake_openstack.Provisioner.return_value.provision.call_count == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("apiHostname", "api.changed.example.org"),
        ("argocdHostname", "argocd.changed.example.org"),
        ("nodeInterface", "enp1s0"),
        ("pythonInterpreter", "/opt/python/bin/python3.13"),
        ("profileName", "reviewed-v2"),
        ("profileRevision", {"uid": "profile-uid", "generation": 2}),
        ("profileRevision", {"uid": "recreated-profile-uid", "generation": 1}),
    ],
)
@pytest.mark.parametrize(
    ("role", "suffix", "vip"),
    [
        ("controller", "controller-01", "10.44.0.10"),
        ("worker", "worker-01", "10.44.0.11"),
        ("jumphost", "jumphost", None),
    ],
)
def test_publication_only_revision_reuses_legacy_server_volume_and_port(
    provisioning_input, field, value, role, suffix, vip,
):
    legacy_data = deepcopy(provisioning_input.data)
    legacy_data.pop("inventory")
    legacy_data.pop("profileRevision")
    legacy_data["schemaVersion"] = 1
    changed_data = deepcopy(provisioning_input.data)
    if field == "profileRevision":
        changed_data[field] = value
    else:
        changed_data["inventory"][field] = value
    changed = ProvisioningInput(changed_data)
    assert changed.input_hash == ProvisioningInput(legacy_data).input_hash
    assert changed.publication_hash != provisioning_input.publication_hash

    uid = legacy_data["cluster"]["uid"]
    name = stable_name("example", suffix)
    machine = legacy_data["openstack"][role]
    image = SimpleNamespace(id="image-id")
    flavor = SimpleNamespace(id="flavor-id", name=machine["flavor"])
    keypair = SimpleNamespace(name="bootstrap-key")
    security_group = SimpleNamespace(id="group-id", name="group-name")
    volume = Volume(
        id="volume-id", name=f"{name}-root", size=machine["rootVolumeGB"], status="in-use",
        metadata={"customer_cluster_uid": uid, "source_image_id": image.id},
        volume_image_metadata={"image_id": image.id}, attachments=[{"server_id": "server-id"}],
    )
    server = Server(
        id="server-id", name=name, status="ACTIVE", key_name=keypair.name,
        metadata={"customer_cluster_uid": uid, "customer_cluster_role": role},
        flavor={"original_name": machine["flavor"]}, attached_volumes=[{"id": volume.id}],
        security_groups=[{"name": security_group.name}],
    )
    port = Port(
        id="port-id", name=f"{name}-port", device_id=server.id, network_id="network-id",
        tags=[f"customer-cluster-uid={uid}"], is_port_security_enabled=True,
        fixed_ips=[{"ip_address": "10.44.0.20", "subnet_id": "subnet-id"}],
        security_group_ids=[security_group.id],
        allowed_address_pairs=[{"ip_address": vip}] if vip else [],
    )
    connection = SimpleNamespace(compute=Mock(), network=Mock(), block_storage=Mock())
    connection.compute.servers.return_value = [server]
    connection.compute.flavors.return_value = [flavor]
    connection.compute.wait_for_server.return_value = server
    connection.compute.get_server.return_value = server
    connection.block_storage.volumes.return_value = [volume]
    connection.network.ports.return_value = [port]
    connection.network.get_port.return_value = port

    for data in (legacy_data, changed_data):
        provisioner = Provisioner(connection, data, ["ssh-ed25519 QUFBQQ=="])
        assert provisioner._server(
            name=name, role=role, machine=data["openstack"][role],
            network=SimpleNamespace(id="network-id"), subnet=SimpleNamespace(id="subnet-id"),
            security_groups=[security_group], image=image, keypair=keypair, vip=vip,
        ) is server

    assert connection.compute.get_server.call_count == 2
    connection.compute.create_server.assert_not_called()
    connection.block_storage.create_volume.assert_not_called()
    connection.network.create_port.assert_not_called()
    connection.network.update_port.assert_not_called()


def test_termination_log_is_structured_json(tmp_path, provisioning_input):
    path = tmp_path / "termination-log"
    result = {
        "schemaVersion": 2,
        "inventoryPath": provisioning_input.inventory_path,
        "policyInventoryPath": provisioning_input.policy_inventory_path,
        "inputHash": provisioning_input.input_hash,
        "inventoryInputHash": provisioning_input.inventory_hash,
        "publicationHash": provisioning_input.publication_hash,
        "inventoryCommit": "a" * 40,
        "apiFloatingIp": "192.0.2.11",
        "ingressFloatingIp": "192.0.2.12",
    }
    worker.write_termination_result(result, path)
    assert json.loads(path.read_text()) == result


def test_worker_source_does_not_execute_configuration_management():
    source = __import__("inspect").getsource(worker)
    assert "ansible-playbook" not in source
    assert "kubespray" not in source.lower()
