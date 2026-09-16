import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from customer_cluster_operator import kube
from customer_cluster_operator.errors import ValidationError
from customer_cluster_operator.kube import (
    bounded,
    cleanup_history,
    condition,
    input_annotations,
    input_config_map,
    inventory_conflict,
    job_result,
    labels,
    provisioning_job,
)


@pytest.fixture
def receipt():
    return {
        "schemaVersion": 2,
        "inventoryPath": "clusters/example/generated/ansible/hosts.yml",
        "policyInventoryPath": "inventory/clusters/example.yml",
        "inventoryCommit": "a" * 40,
        "inputHash": "b" * 64,
        "inventoryInputHash": "c" * 64,
        "publicationHash": "d" * 64,
        "apiFloatingIp": "192.0.2.11",
        "ingressFloatingIp": "192.0.2.12",
    }


def result_pod(message, *, uid="job-uid", exit_code=0, container="provision"):
    return SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[SimpleNamespace(uid=uid)]),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    name=container,
                    state=SimpleNamespace(
                        terminated=SimpleNamespace(exit_code=exit_code, message=message)
                    ),
                )
            ]
        ),
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
    assert config_map.metadata.labels["customer-clusters.sunet.se/publication-hash"] == (
        provisioning_input.publication_hash[:63]
    )
    assert config_map.metadata.annotations == input_annotations(provisioning_input)
    assert json.loads(config_map.data["input.json"])["inventory"] == (
        provisioning_input.data["inventory"]
    )
    assert json.loads(config_map.data["input.json"])["profileRevision"] == {
        "uid": "profile-uid", "generation": 1,
    }


def test_input_annotations_bind_all_hashes_and_both_paths(provisioning_input):
    assert input_annotations(provisioning_input) == {
        "customer-clusters.sunet.se/input-hash": provisioning_input.input_hash,
        "customer-clusters.sunet.se/publication-hash": provisioning_input.publication_hash,
        "customer-clusters.sunet.se/inventory-input-hash": provisioning_input.inventory_hash,
        "customer-clusters.sunet.se/inventory-path": (
            "clusters/example/generated/ansible/hosts.yml"
        ),
        "customer-clusters.sunet.se/policy-inventory-path": "inventory/clusters/example.yml",
    }


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
    assert job.metadata.annotations == input_annotations(provisioning_input)
    assert job.metadata.labels == job.spec.template.metadata.labels == labels(
        body["metadata"]["uid"], provisioning_input.input_hash, provisioning_input.publication_hash
    )
    mounted_input = next(item for item in pod.volumes if item.name == "input")
    assert mounted_input.config_map.name == "job-name-input"
    assert container.command == ["python", "-m", "customer_cluster_operator.worker"]
    assert pod.security_context.seccomp_profile.type == "RuntimeDefault"


def test_labels_shorten_hash_but_not_uid():
    result = labels("uid", "a" * 64, "b" * 64)
    assert result["customer-clusters.sunet.se/input-hash"] == "a" * 63
    assert result["customer-clusters.sunet.se/publication-hash"] == "b" * 63
    assert result["customer-clusters.sunet.se/cluster-uid"] == "uid"
    assert "customer-clusters.sunet.se/publication-hash" not in labels("uid", "a" * 64)


def test_condition_preserves_time_when_status_does_not_transition():
    previous = [{"type": "Ready", "status": "False", "lastTransitionTime": "old"}]
    assert (
        condition("Ready", "False", "NewReason", previous=previous)["lastTransitionTime"] == "old"
    )


@pytest.mark.parametrize("commit_length", [40, 64])
def test_job_result_reads_owned_pod(receipt, commit_length):
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    receipt["inventoryCommit"] = "a" * commit_length
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    result = job_result(api, "openstack-operator", job)
    assert result == {key: value for key, value in receipt.items() if key != "schemaVersion"}
    api.list_namespaced_pod.assert_called_once_with(
        "openstack-operator", label_selector="job-name=job"
    )


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


def test_job_result_accepts_identical_retry_results(receipt):
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    message = json.dumps(receipt, separators=(",", ":"))
    equivalent = json.dumps(dict(reversed(receipt.items())), indent=2)
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(message), result_pod(equivalent)]
    )
    assert job_result(api, "openstack-operator", job)["inventoryCommit"] == "a" * 40


@pytest.mark.parametrize(
    "changed",
    [
        {"inventoryCommit": "e" * 40},
        {"inputHash": "e" * 64},
        {"inventoryInputHash": "e" * 64},
        {"publicationHash": "e" * 64},
        {"apiFloatingIp": "192.0.2.21"},
        {"ingressFloatingIp": "192.0.2.22"},
        {
            "inventoryPath": "clusters/other/generated/ansible/hosts.yml",
            "policyInventoryPath": "inventory/clusters/other.yml",
        },
    ],
)
def test_job_result_rejects_mixed_successful_retry_receipts(receipt, changed):
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt)), result_pod(json.dumps(receipt | changed))]
    )
    with pytest.raises(ValidationError, match="conflicting"):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize("key", ["apiFloatingIp", "ingressFloatingIp"])
@pytest.mark.parametrize("value", ["2001:db8::1", "invalid", "192.0.2.11/32", 3221225995, None])
def test_job_result_requires_ipv4_endpoint_addresses(receipt, key, value):
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    receipt[key] = value
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    with pytest.raises(ValidationError, match=key):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize("schema", [1, 3, "2", 2.0, True, None])
def test_job_result_rejects_unsupported_schema(receipt, schema):
    receipt["schemaVersion"] = schema
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError, match="schema v2"):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize(
    "missing",
    [
        "schemaVersion", "inputHash", "inventoryInputHash", "publicationHash",
        "policyInventoryPath", "inventoryPath", "inventoryCommit", "apiFloatingIp",
        "ingressFloatingIp",
    ],
)
def test_job_result_requires_every_publication_receipt_field(receipt, missing):
    del receipt[missing]
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    message = "schema v2" if missing == "schemaVersion" else missing
    with pytest.raises(ValidationError, match=message):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize("key", ["inputHash", "inventoryInputHash", "publicationHash"])
@pytest.mark.parametrize("value", ["a" * 63, "A" * 64, "g" * 64, 123, None])
def test_job_result_requires_full_lowercase_sha256_hashes(receipt, key, value):
    receipt[key] = value
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError, match=key):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("inventoryPath", "clusters/../generated/ansible/hosts.yml"),
        ("inventoryPath", "clusters/Example/generated/ansible/hosts.yml"),
        ("inventoryPath", "clusters/example/hosts.yml"),
        ("policyInventoryPath", "inventory/clusters/other.yml"),
        ("policyInventoryPath", "inventory/clusters/../example.yml"),
        ("inventoryCommit", "a" * 39),
        ("inventoryCommit", "a" * 41),
        ("inventoryCommit", "A" * 40),
    ],
)
def test_job_result_rejects_invalid_paths_and_commit(receipt, key, value):
    receipt[key] = value
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[result_pod(json.dumps(receipt))]
    )
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError, match=key):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize("include_current", [False, True])
def test_job_result_rejects_legacy_receipts_even_alongside_current(receipt, include_current):
    legacy = {key: receipt[key] for key in (
        "inventoryPath", "inventoryCommit", "apiFloatingIp", "ingressFloatingIp",
    )}
    pods = [result_pod(json.dumps(legacy))]
    if include_current:
        pods.append(result_pod(json.dumps(receipt)))
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError, match="schema v2"):
        job_result(api, "openstack-operator", job)


def test_job_result_ignores_failed_unowned_and_other_container_results(receipt):
    message = json.dumps(receipt)
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[
        result_pod("malformed", exit_code=1),
        result_pod("malformed", uid="foreign-job"),
        result_pod("malformed", container="sidecar"),
        result_pod(message),
    ])
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    parsed = job_result(api, "openstack-operator", job)
    assert parsed["publicationHash"] == receipt["publicationHash"]


@pytest.mark.parametrize("message", ["invalid", "[]", "null"])
def test_job_result_rejects_malformed_successful_result(message):
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[result_pod(message)])
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    with pytest.raises(ValidationError):
        job_result(api, "openstack-operator", job)


@pytest.mark.parametrize(
    ("uid", "exit_code", "container", "message", "expected"),
    [
        ("job-uid", 1, "provision", '{"errorCode":"InventoryConflict"}', True),
        ("foreign", 1, "provision", '{"errorCode":"InventoryConflict"}', False),
        ("job-uid", 0, "provision", '{"errorCode":"InventoryConflict"}', False),
        ("job-uid", 1, "sidecar", '{"errorCode":"InventoryConflict"}', False),
        ("job-uid", 1, "provision", '{"errorCode":"OtherFailure"}', False),
        ("job-uid", 1, "provision", '{"message":"InventoryConflict"}', False),
        ("job-uid", 1, "provision", "InventoryConflict", False),
        ("job-uid", 1, "provision", "[]", False),
        ("job-uid", 1, "provision", None, False),
    ],
)
def test_inventory_conflict_requires_owned_failed_worker_code(
    uid, exit_code, container, message, expected,
):
    api = Mock()
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[
        result_pod(message, uid=uid, exit_code=exit_code, container=container),
    ])
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid="job-uid"))
    assert inventory_conflict(api, "openstack-operator", job) is expected
    api.read_namespaced_pod_log.assert_not_called()


def test_inventory_conflict_without_job_uid_does_not_read_pods():
    api = Mock()
    job = SimpleNamespace(metadata=SimpleNamespace(name="job", uid=None))
    assert inventory_conflict(api, "openstack-operator", job) is False
    api.list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize(
    ("status", "finished"),
    [
        pytest.param(None, False, id="missing-status"),
        pytest.param(client.V1JobStatus(), False, id="empty-status"),
        pytest.param(client.V1JobStatus(active=0, conditions=[]), False, id="pending-no-pods"),
        pytest.param(client.V1JobStatus(active=1), False, id="running"),
        pytest.param(
            client.V1JobStatus(active=0, failed=1, conditions=[]), False, id="retry-backoff",
        ),
        pytest.param(client.V1JobStatus(active=0, failed=1, conditions=[
            client.V1JobCondition(type="Failed", status="False"),
        ]), False, id="failed-false"),
        pytest.param(client.V1JobStatus(active=0, conditions=[
            client.V1JobCondition(type="Complete", status="Unknown"),
        ]), False, id="complete-unknown"),
        pytest.param(client.V1JobStatus(active=0, failed=1, conditions=[
            client.V1JobCondition(type="FailureTarget", status="True"),
        ]), False, id="failure-target-is-not-terminal"),
        pytest.param(client.V1JobStatus(active=0, conditions=[
            client.V1JobCondition(type="SuccessCriteriaMet", status="True"),
        ]), False, id="success-criteria-met-is-not-terminal"),
        pytest.param(client.V1JobStatus(succeeded=1), False, id="success-counter-only"),
        pytest.param(
            client.V1JobStatus(active=0, terminating=0, succeeded=1, conditions=[]), False,
            id="success-without-terminal-condition",
        ),
        pytest.param(
            client.V1JobStatus(active=1, terminating=0, succeeded=1, conditions=[]), False,
            id="success-with-active-pod",
        ),
        pytest.param(
            client.V1JobStatus(active=0, terminating=1, succeeded=1, conditions=[]), False,
            id="success-with-terminating-pod",
        ),
        pytest.param(client.V1JobStatus(active=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]), False, id="complete-with-active-pod"),
        pytest.param(client.V1JobStatus(terminating=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]), False, id="complete-with-terminating-pod"),
        pytest.param(client.V1JobStatus(active=1, conditions=[
            client.V1JobCondition(type="Failed", status="True"),
        ]), False, id="failed-with-active-pod"),
        pytest.param(client.V1JobStatus(terminating=1, conditions=[
            client.V1JobCondition(type="Failed", status="True"),
        ]), False, id="failed-with-terminating-pod"),
        pytest.param(client.V1JobStatus(active=0, terminating=0, succeeded=1, conditions=[
            client.V1JobCondition(type="SuccessCriteriaMet", status="True"),
        ]), False, id="success-criteria-and-counter-without-terminal-condition"),
        pytest.param(client.V1JobStatus(conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]), True, id="complete-condition"),
        pytest.param(client.V1JobStatus(conditions=[
            client.V1JobCondition(type="Failed", status="True"),
        ]), True, id="failed-condition"),
    ],
)
def test_job_finished_uses_terminal_evidence_not_active_or_failed_pod_counts(status, finished):
    assert kube.job_finished(client.V1Job(status=status)) is finished


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(None, id="missing-status"),
        pytest.param(client.V1JobStatus(active=0, conditions=[]), id="pending-no-pods"),
        pytest.param(client.V1JobStatus(active=0, failed=1, conditions=[]), id="retry-backoff"),
        pytest.param(client.V1JobStatus(succeeded=1, conditions=[]), id="success-counter-only"),
        pytest.param(client.V1JobStatus(active=1, succeeded=1), id="success-with-active-pod"),
        pytest.param(client.V1JobStatus(terminating=1, succeeded=1), id="success-with-terminating"),
        pytest.param(client.V1JobStatus(active=1, succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]), id="complete-with-active-pod"),
        pytest.param(client.V1JobStatus(terminating=1, succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]), id="complete-with-terminating-pod"),
    ],
)
def test_cleanup_preserves_unfinished_job_and_input_regardless_of_history_age(status):
    owner = client.V1OwnerReference(
        api_version="customer-clusters.sunet.se/v1alpha1", kind="ManagedCluster",
        name="example", uid="cluster-uid",
    )
    jobs = [client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name, owner_references=[owner],
            creation_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        status=client.V1JobStatus(succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]),
    ) for name in ("current", "recent")]
    unfinished = client.V1Job(
        metadata=client.V1ObjectMeta(
            name="unfinished", owner_references=[owner],
            creation_timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        ),
        status=status,
    )
    jobs.append(unfinished)
    core = Mock()
    core.list_namespaced_config_map.return_value = SimpleNamespace(items=[
        client.V1ConfigMap(metadata=client.V1ObjectMeta(
            name=f"{job.metadata.name}-input", owner_references=[owner],
        )) for job in jobs
    ])
    batch = Mock()
    retained = cleanup_history(
        core_api=core, batch_api=batch, namespace="openstack-operator", cluster_uid="cluster-uid",
        jobs=jobs, current_job_name="current", status_job_name="current",
    )
    assert {job.metadata.name for job in retained} == {"current", "recent", "unfinished"}
    batch.delete_namespaced_job.assert_not_called()
    core.delete_namespaced_config_map.assert_not_called()
    core.list_namespaced_pod.assert_not_called()


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
                conditions=(
                    [client.V1JobCondition(type="Complete", status="True")] if succeeded else []
                ),
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
        status=client.V1JobStatus(succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ]),
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
