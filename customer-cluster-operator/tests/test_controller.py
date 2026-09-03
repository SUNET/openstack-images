from types import SimpleNamespace
from unittest.mock import Mock

import kopf
import pytest
from kubernetes.client.exceptions import ApiException

from customer_cluster_operator import controller


def APIs(
    profile,
    *,
    jobs=None,
    project_status=None,
    project_spec=None,
    project_generation=2,
    pods=None,
):
    custom = Mock()
    custom.get_cluster_custom_object.return_value = {"spec": profile}
    custom.get_namespaced_custom_object.return_value = {
        "metadata": {"generation": project_generation},
        "status": project_status
        or {
            "phase": "Ready",
            "projectId": "project-id",
            "observedGeneration": project_generation,
        },
        "spec": project_spec
        or {"name": "customer-example", "contractNumber": "C-123", "managed": True},
    }
    core = Mock()
    core.read_namespaced_secret.return_value = SimpleNamespace(
        data={"clouds.yaml": "not-inspected", "token": "not-inspected"}
    )
    core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"authorized_keys": "not-inspected"}
    )
    core.list_namespaced_pod.return_value = SimpleNamespace(items=pods or [])
    core.list_namespaced_config_map.return_value = SimpleNamespace(items=[])
    batch = Mock()
    batch.list_namespaced_job.return_value = SimpleNamespace(items=jobs or [])
    return custom, core, batch


def reconcile(monkeypatch, spec, body, apis, status=None, namespace="openstack-operator"):
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/worker:1")
    monkeypatch.setattr(controller, "get_apis", lambda: apis)
    monkeypatch.setattr(controller, "_verification_bucket", lambda settings: 7)
    patch = kopf.Patch()
    controller.reconcile(
        spec=spec,
        status=status or {},
        patch=patch,
        body=body,
        namespace=namespace,
        name="example",
    )
    return patch


def test_wrong_namespace_is_rejected_before_api_calls(monkeypatch, spec, profile, body):
    apis = APIs(profile)
    body["metadata"]["namespace"] = "customer"
    patch = reconcile(monkeypatch, spec, body, apis, namespace="customer")
    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    apis[0].get_cluster_custom_object.assert_not_called()


def test_waits_for_project(monkeypatch, spec, profile, body):
    apis = APIs(profile, project_status={"phase": "Provisioning"})
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "PendingProject"
    apis[2].create_namespaced_job.assert_not_called()


def test_waits_for_missing_project(monkeypatch, spec, profile, body):
    apis = APIs(profile)
    apis[0].get_namespaced_custom_object.side_effect = ApiException(status=404)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "PendingProject"
    apis[2].list_namespaced_job.assert_not_called()


@pytest.mark.parametrize(
    "project_status",
    [
        {"phase": "Ready", "projectId": "project-id"},
        {"phase": "Ready", "projectId": "project-id", "observedGeneration": 1},
        {"phase": "Ready", "projectId": "project-id", "observedGeneration": "2"},
        {"phase": "Ready", "projectId": "", "observedGeneration": 2},
    ],
)
def test_waits_for_project_current_generation(monkeypatch, spec, profile, body, project_status):
    apis = APIs(profile, project_status=project_status)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "PendingProject"
    apis[2].list_namespaced_job.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("project_generation", [None, "2", True, 0])
def test_waits_for_invalid_project_generation(monkeypatch, spec, profile, body, project_generation):
    apis = APIs(profile, project_generation=project_generation)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "PendingProject"
    apis[2].list_namespaced_job.assert_not_called()


def test_suspend_true_does_not_call_apis(monkeypatch, spec, profile, body):
    spec["suspend"] = True
    apis = APIs(profile)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "Suspended"
    apis[0].get_cluster_custom_object.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def test_suspend_absent_provisions_when_project_is_ready(monkeypatch, spec, profile, body):
    assert "suspend" not in spec
    apis = APIs(profile)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    apis[2].create_namespaced_job.assert_called_once()


def test_creates_one_configmap_and_job(monkeypatch, spec, profile, body):
    apis = APIs(profile)
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert len(patch.status["inputHash"]) == 64
    apis[1].create_namespaced_config_map.assert_called_once()
    apis[2].create_namespaced_job.assert_called_once()
    assert apis[1].read_namespaced_secret.call_count == 2
    apis[1].read_namespaced_config_map.assert_called_once_with(
        "cluster-authorized-keys", "openstack-operator"
    )


@pytest.mark.parametrize("resource", ["credentials", "git", "ssh"])
@pytest.mark.parametrize("failure", ["missing", "missing-key"])
def test_missing_prerequisite_waits_without_creating_resources(
    monkeypatch, spec, profile, body, resource, failure
):
    apis = APIs(profile)
    core = apis[1]
    if resource in {"credentials", "git"}:
        target = "clouds" if resource == "credentials" else "cluster-git"

        def read_secret(name, namespace):
            if name == target:
                if failure == "missing":
                    raise ApiException(status=404)
                return SimpleNamespace(data={})
            return SimpleNamespace(data={"token": "not-inspected"})

        core.read_namespaced_secret.side_effect = read_secret
    elif failure == "missing":
        core.read_namespaced_config_map.side_effect = ApiException(status=404)
    else:
        core.read_namespaced_config_map.return_value = SimpleNamespace(data={})

    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "PendingPrerequisites"
    assert patch.status["conditions"][0]["reason"] == "PrerequisitesNotReady"
    core.create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def test_prerequisite_api_failure_propagates(monkeypatch, spec, profile, body):
    apis = APIs(profile)
    apis[1].read_namespaced_secret.side_effect = ApiException(status=503)
    with pytest.raises(ApiException) as error:
        reconcile(monkeypatch, spec, body, apis)
    assert error.value.status == 503
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def test_ready_status_is_verified_from_owned_job_result(
    monkeypatch, spec, profile, body, provisioning_input
):
    name = controller.job_name(
        "example",
        body["metadata"]["uid"],
        provisioning_input.input_hash,
        body["metadata"]["generation"],
        7,
    )
    job_uid = "job-uid"
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=job_uid,
            owner_references=[SimpleNamespace(uid=body["metadata"]["uid"])],
        ),
        status=SimpleNamespace(active=None, failed=None, succeeded=1, conditions=[]),
    )
    result = (
        '{"inventoryPath":"clusters/example/generated/ansible/hosts.yml",'
        '"inventoryCommit":"'
        + "a" * 40
        + '","apiFloatingIp":"192.0.2.11","ingressFloatingIp":"192.0.2.12"}'
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid=job_uid)]),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    name="provision",
                    state=SimpleNamespace(terminated=SimpleNamespace(exit_code=0, message=result)),
                )
            ]
        ),
    )
    spec["displayName"] = "New Display Name"
    apis = APIs(profile, jobs=[job], pods=[pod])
    patch = reconcile(
        monkeypatch,
        spec,
        body,
        apis,
        status={"phase": "VirtualMachinesReady", "inputHash": provisioning_input.input_hash},
    )
    assert patch.status["phase"] == "VirtualMachinesReady"
    assert patch.status["inventoryCommit"] == "a" * 40
    assert patch.status["apiFloatingIp"] == "192.0.2.11"
    assert patch.status["ingressFloatingIp"] == "192.0.2.12"
    assert patch.status["lastVerifiedAt"].endswith("Z")
    apis[1].list_namespaced_pod.assert_called_once()
    apis[2].create_namespaced_job.assert_not_called()


def test_infrastructure_drift_is_rejected(monkeypatch, spec, profile, body):
    profile["openstack"]["worker"]["rootVolumeGB"] += 1
    apis = APIs(profile)
    patch = reconcile(
        monkeypatch,
        spec,
        body,
        apis,
        status={"phase": "VirtualMachinesReady", "inputHash": "a" * 64},
    )
    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InfrastructureDriftUnsupported"
    apis[2].create_namespaced_job.assert_not_called()


def test_active_different_job_is_rejected(monkeypatch, spec, profile, body):
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name="old-job",
            owner_references=[SimpleNamespace(uid=body["metadata"]["uid"])],
        ),
        status=SimpleNamespace(active=1, failed=None, succeeded=None, conditions=[]),
    )
    patch = reconcile(monkeypatch, spec, body, APIs(profile, jobs=[job]))
    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"


def test_failed_job_message_is_bounded(monkeypatch, spec, profile, body, provisioning_input):
    name = controller.job_name(
        "example",
        body["metadata"]["uid"],
        provisioning_input.input_hash,
        body["metadata"]["generation"],
        7,
    )
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            owner_references=[SimpleNamespace(uid=body["metadata"]["uid"])],
        ),
        status=SimpleNamespace(
            active=None,
            failed=1,
            succeeded=None,
            conditions=[SimpleNamespace(type="Failed", status="True", message="x" * 1000)],
        ),
    )
    patch = reconcile(monkeypatch, spec, body, APIs(profile, jobs=[job]))
    assert patch.status["phase"] == "Failed"
    assert len(patch.status["conditions"][0]["message"]) == 512


def test_project_identity_must_match(monkeypatch, spec, profile, body):
    apis = APIs(
        profile,
        project_spec={"name": "other", "contractNumber": "C-123", "managed": True},
    )
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["phase"] == "Failed"
    assert "projectName" in patch.status["conditions"][0]["message"]
    call = apis[0].get_namespaced_custom_object.call_args
    assert call.args[2] == "customer-projects"


def test_project_must_be_managed_and_match_contract(monkeypatch, spec, profile, body):
    apis = APIs(
        profile,
        project_spec={
            "name": "customer-example",
            "contractNumber": "wrong",
            "managed": False,
        },
    )
    patch = reconcile(monkeypatch, spec, body, apis)
    assert "contractNumber" in patch.status["conditions"][0]["message"]

    apis = APIs(
        profile,
        project_spec={
            "name": "customer-example",
            "contractNumber": "C-123",
            "managed": False,
        },
    )
    patch = reconcile(monkeypatch, spec, body, apis)
    assert "managed true" in patch.status["conditions"][0]["message"]


def test_condition_time_is_preserved_without_transition(monkeypatch, spec, profile, body):
    previous = "2026-01-01T00:00:00Z"
    status = {
        "conditions": [
            {
                "type": "Ready",
                "status": "False",
                "reason": "ProjectNotReady",
                "message": "old",
                "lastTransitionTime": previous,
            }
        ]
    }
    patch = reconcile(
        monkeypatch,
        spec,
        body,
        APIs(profile, project_status={"phase": "Provisioning"}),
        status=status,
    )
    assert patch.status["conditions"][0]["lastTransitionTime"] == previous


def test_new_generation_launches_idempotent_verification_job(
    monkeypatch, spec, profile, body, provisioning_input
):
    old_job = SimpleNamespace(
        metadata=SimpleNamespace(
            name=controller.job_name(
                "example",
                body["metadata"]["uid"],
                provisioning_input.input_hash,
                3,
                7,
            ),
            owner_references=[SimpleNamespace(uid=body["metadata"]["uid"])],
        ),
        status=SimpleNamespace(active=None, failed=None, succeeded=1, conditions=[]),
    )
    apis = APIs(profile, jobs=[old_job])
    patch = reconcile(
        monkeypatch,
        spec,
        body,
        apis,
        status={
            "phase": "VirtualMachinesReady",
            "observedGeneration": 3,
            "inputHash": provisioning_input.input_hash,
        },
    )
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["jobName"].endswith("-g4-v7")
    apis[2].create_namespaced_job.assert_called_once()


def test_new_time_bucket_launches_periodic_verification_job(
    monkeypatch, spec, profile, body, provisioning_input
):
    previous = SimpleNamespace(
        metadata=SimpleNamespace(
            name=controller.job_name(
                "example",
                body["metadata"]["uid"],
                provisioning_input.input_hash,
                body["metadata"]["generation"],
                6,
            ),
            owner_references=[SimpleNamespace(uid=body["metadata"]["uid"])],
        ),
        status=SimpleNamespace(active=None, failed=None, succeeded=1, conditions=[]),
    )
    apis = APIs(profile, jobs=[previous])
    patch = reconcile(
        monkeypatch,
        spec,
        body,
        apis,
        status={
            "phase": "VirtualMachinesReady",
            "observedGeneration": body["metadata"]["generation"],
            "inputHash": provisioning_input.input_hash,
            "lastVerifiedAt": "2026-09-02T09:45:00Z",
        },
    )
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["jobName"].endswith("-v7")
    apis[2].create_namespaced_job.assert_called_once()


def test_main_uses_module_and_namespace(monkeypatch):
    monkeypatch.setenv("WORKER_IMAGE", "worker:1")
    called = {}

    def execvp(executable, arguments):
        called.update(executable=executable, arguments=arguments)
        raise RuntimeError("stop")

    monkeypatch.setattr(controller.os, "execvp", execvp)
    try:
        controller.main()
    except RuntimeError:
        pass
    assert called["executable"] == "kopf"
    assert called["arguments"][-2:] == ["--module", "customer_cluster_operator.controller"]
    assert "openstack-operator" in called["arguments"]
