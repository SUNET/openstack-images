import json

import pytest

from customer_cluster_operator import worker
from customer_cluster_operator.errors import ValidationError


def test_load_input_rejects_unknown_schema(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"schemaVersion": 2}))
    with pytest.raises(ValidationError, match="unsupported"):
        worker.load_input(path)


def test_worker_provisions_and_only_publishes_inventory(monkeypatch, provisioning_input):
    resources = {
        "jumphost": {"name": "jump", "floating_ip": "192.0.2.10"},
        "controllers": [{"name": "controller", "ip": "10.0.0.10"}] * 3,
        "workers": [{"name": "worker", "ip": "10.0.0.20"}] * 6,
        "api_vip": "10.44.0.10",
        "ingress_vip": "10.44.0.11",
        "api_floating_ip": "192.0.2.11",
        "ingress_floating_ip": "192.0.2.12",
    }
    monkeypatch.setattr(worker, "read_public_keys", lambda path: ["ssh-ed25519 QUFBQQ=="])
    monkeypatch.setattr(worker, "scoped_connection", lambda data, path: "connection")
    provisioner = type("FakeProvisioner", (), {"provision": lambda self: resources})
    monkeypatch.setattr(worker, "Provisioner", lambda *args: provisioner())
    published = {}

    def publish(config, slug, inventory, token):
        published.update(config=config, slug=slug, inventory=inventory, token=token)
        return "clusters/example/generated/ansible/hosts.yml", "a" * 40

    monkeypatch.setattr(worker, "publish_inventory", publish)
    result = worker.run(provisioning_input.data, token="secret", clouds_file="/clouds")
    assert result["controllers"] == 3
    assert result["workers"] == 6
    assert result["inventoryPath"].endswith("hosts.yml")
    assert result["inventoryCommit"] == "a" * 40
    assert result["apiFloatingIp"] == "192.0.2.11"
    assert result["ingressFloatingIp"] == "192.0.2.12"
    assert published["token"] == "secret"
    assert "ProxyJump" in published["inventory"]


def test_termination_log_is_structured_json(tmp_path):
    path = tmp_path / "termination-log"
    result = {"inventoryPath": "clusters/example/hosts.yml", "inventoryCommit": "a" * 40}
    worker.write_termination_result(result, path)
    assert json.loads(path.read_text()) == result


def test_worker_source_does_not_execute_configuration_management():
    source = __import__("inspect").getsource(worker)
    assert "ansible-playbook" not in source
    assert "kubespray" not in source.lower()
