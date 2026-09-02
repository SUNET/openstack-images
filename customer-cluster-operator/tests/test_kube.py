from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.kube import (
    bounded,
    cleanup_history,
    condition,
    input_config_map,
    job_result,
    labels,
    provisioning_job,
)


def test_bounded_flattens_and_limits_messages():
    assert bounded("a\n  b") == "a b"
    assert len(bounded("x" * 1000)) == 512


def test_input_config_map_is_immutable_and_owned(body, provisioning_input):
    config_map = input_config_map(name="job-name", body=body, provisioning_input=provisioning_input)
    assert config_map.immutable is True
    assert config_map.metadata.namespace == "openstack-operator"
    assert config_map.metadata.owner_references[0].uid == body["metadata"]["uid"]
    assert len(config_map.metadata.name) <= 63
    assert config_map.data["input.json"] == provisioning_input.canonical_json
    assert len(config_map.metadata.labels["customer-clusters.sunet.se/input-hash"]) == 63
    assert (
        config_map.metadata.annotations["customer-clusters.sunet.se/input-hash"]
        == provisioning_input.input_hash
    )


def test_job_is_hardened_and_uses_refs(body, provisioning_input):
    job = provisioning_job(
        name="job-name",
        body=body,
        provisioning_input=provisioning_input,
        worker_image="registry.example/worker:1",
        service_account="worker-sa",
    )
    pod = job.spec.template.spec
    container = pod.containers[0]
    assert pod.service_account_name == "worker-sa"
    assert pod.automount_service_account_token is False
    assert pod.security_context.run_as_non_root is True
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.capabilities.drop == ["ALL"]
    token = next(item for item in container.env if item.name == "GIT_TOKEN")
    assert token.value_from.secret_key_ref.name == "cluster-git"
    assert token.value is None
    clouds = next(item for item in pod.volumes if item.name == "clouds")
    assert clouds.secret.secret_name == "clouds"
    assert clouds.secret.items[0].key == "clouds.yaml"
    assert job.spec.backoff_limit == 3


def test_labels_shorten_hash_but_not_uid():
    result = labels("uid", "a" * 64)
    assert result["customer-clusters.sunet.se/input-hash"] == "a" * 63
    assert result["customer-clusters.sunet.se/cluster-uid"] == "uid"


def test_condition_preserves_time_when_status_does_not_transition():
    previous = [{"type": "Ready", "status": "False", "lastTransitionTime": "old"}]
    assert (
        condition("Ready", "False", "NewReason", previous=previous)["lastTransitionTime"] == "old"
    )


def test_job_result_reads_owned_pod():
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    message = (
        '{"inventoryPath":"clusters/example/generated/ansible/hosts.yml",'
        '"inventoryCommit":"' + "a" * 40 + '"}'
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid="job-uid")]),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    name="provision",
                    state=SimpleNamespace(terminated=SimpleNamespace(exit_code=0, message=message)),
                )
            ]
        ),
    )
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    result = job_result(api, "openstack-operator", job)
    assert result["inventoryCommit"] == "a" * 40


def test_job_result_rejects_unowned_pod():
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid="another-job")])
            )
        ]
    )
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError, match="no successful"):
        job_result(api, "openstack-operator", job)


def test_job_result_accepts_identical_retry_results():
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    message = (
        '{"inventoryPath":"clusters/example/generated/ansible/hosts.yml",'
        '"inventoryCommit":"' + "a" * 40 + '"}'
    )
    equivalent = (
        '{"inventoryCommit":"'
        + "a" * 40
        + '","inventoryPath":"clusters/example/generated/ansible/hosts.yml"}'
    )

    def pod(value):
        return SimpleNamespace(
            metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid="job-uid")]),
            status=SimpleNamespace(
                container_statuses=[
                    SimpleNamespace(
                        name="provision",
                        state=SimpleNamespace(
                            terminated=SimpleNamespace(exit_code=0, message=value)
                        ),
                    )
                ]
            ),
        )

    api.list_namespaced_pod.return_value = SimpleNamespace(items=[pod(message), pod(equivalent)])
    assert job_result(api, "openstack-operator", job)["inventoryCommit"] == "a" * 40
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[pod(message), pod(message.replace("a" * 40, "b" * 40))]
    )
    with pytest.raises(ValidationError, match="conflicting"):
        job_result(api, "openstack-operator", job)


def test_cleanup_bounds_completed_jobs_configmaps_and_pods():
    uid = "cluster-uid"
    now = datetime.now(UTC)

    def owner(value):
        return client.V1OwnerReference(
            api_version="customer-clusters.sunet.se/v1alpha1",
            kind="ManagedCluster",
            name="example",
            uid=value,
        )

    def job(name, age, active=None, succeeded=1):
        return client.V1Job(
            metadata=client.V1ObjectMeta(
                name=name,
                creation_timestamp=now - timedelta(minutes=age),
                owner_references=[owner(uid)],
            ),
            status=client.V1JobStatus(
                active=active,
                succeeded=succeeded,
                conditions=[],
            ),
        )

    jobs = [
        job("current", 0),
        job("recent", 15),
        job("old", 30),
        job("active", 60, active=1, succeeded=None),
    ]
    config_maps = [
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"{name}-input",
                owner_references=[owner(uid)],
            )
        )
        for name in ("current", "recent", "old", "active", "orphan")
    ]
    core_api = Mock()
    core_api.list_namespaced_config_map.return_value = SimpleNamespace(items=config_maps)
    batch_api = Mock()
    retained = cleanup_history(
        core_api=core_api,
        batch_api=batch_api,
        namespace="openstack-operator",
        cluster_uid=uid,
        jobs=jobs,
        current_job_name="current",
        status_job_name="current",
    )
    assert {item.metadata.name for item in retained} == {"current", "recent", "active"}
    batch_api.delete_namespaced_job.assert_called_once_with(
        "old", "openstack-operator", propagation_policy="Background"
    )
    deleted_maps = {call.args[0] for call in core_api.delete_namespaced_config_map.call_args_list}
    assert deleted_maps == {"old-input", "orphan-input"}
    assert "active" not in {call.args[0] for call in batch_api.mock_calls}


def test_cleanup_refuses_unowned_jobs():
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name="foreign",
            owner_references=[
                client.V1OwnerReference(
                    api_version="customer-clusters.sunet.se/v1alpha1",
                    kind="ManagedCluster",
                    name="other",
                    uid="another-cluster",
                )
            ],
        ),
        status=client.V1JobStatus(succeeded=1),
    )
    with pytest.raises(ValidationError, match="not owned"):
        cleanup_history(
            core_api=Mock(),
            batch_api=Mock(),
            namespace="openstack-operator",
            cluster_uid="cluster-uid",
            jobs=[job],
            current_job_name="current",
            status_job_name=None,
        )
