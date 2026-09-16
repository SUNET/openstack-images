"""Exercise schema upgrades and publication revisions through real reconciliation."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from test_controller import APIs, reconcile

from customer_cluster_operator import models, worker
from customer_cluster_operator.kube import input_config_map, labels, provisioning_job
from customer_cluster_operator.models import ProvisioningInput, build_input, job_name

GOLDEN_V015_INPUT_HASH = "e2a279ed8c80059d96a33fffbd9e90841c45a736c8fb28dd58895897d2db285e"
ANNOTATION_PREFIX = "customer-clusters.sunet.se/"
ANNOTATION_FIELDS = (
    "input-hash", "publication-hash", "inventory-input-hash", "inventory-path",
    "policy-inventory-path",
)
PUBLICATION_STATUS_FIELDS = {
    "inventoryInputHash", "publicationHash", "policyInventoryPath", "inventoryCommit",
    "lastVerifiedAt", "apiFloatingIp", "ingressFloatingIp",
}
UNFINISHED_SUCCESS_STATES = (
    "success-counter", "success-active", "success-terminating", "complete-active",
    "complete-terminating",
)
UNFINISHED_STATES = (
    "absent", "pending", "backoff", "failed-false", "failure-target", "running",
    *UNFINISHED_SUCCESS_STATES, "failed-active", "failed-terminating",
)


def desired_input(spec, profile, body, *, profile_uid="profile-uid", profile_generation=1):
    return build_input(
        spec=spec,
        profile=profile,
        profile_revision={"uid": profile_uid, "generation": profile_generation},
        uid=body["metadata"]["uid"],
        slug=body["metadata"]["name"],
        namespace=body["metadata"]["namespace"],
        project_id="project-id",
        operator_namespace="openstack-operator",
    )


def revision_job(body, desired, *, legacy=False, active=None, succeeded=None, failed=None):
    name = job_name(
        body["metadata"]["name"],
        body["metadata"]["uid"],
        desired.input_hash if legacy else desired.publication_hash,
        body["metadata"]["generation"],
        7,
    )
    job = provisioning_job(
        name=name,
        body=body,
        provisioning_input=desired,
        worker_image="registry.example/worker:1",
        service_account="worker-sa",
    )
    job.metadata.uid = f"{name}-uid"
    conditions = []
    if succeeded:
        conditions = [client.V1JobCondition(type="Complete", status="True")]
    elif failed:
        conditions = [client.V1JobCondition(type="Failed", status="True")]
    job.status = client.V1JobStatus(
        active=active, succeeded=succeeded, failed=failed, conditions=conditions,
    )
    if legacy:
        job.metadata.annotations = {f"{ANNOTATION_PREFIX}input-hash": desired.input_hash}
        job.metadata.labels = labels(body["metadata"]["uid"], desired.input_hash)
        job.spec.template.metadata.labels = dict(job.metadata.labels)
    return job


def receipt(desired):
    return {
        "schemaVersion": 2,
        "inputHash": desired.input_hash,
        "inventoryInputHash": desired.inventory_hash,
        "publicationHash": desired.publication_hash,
        "inventoryPath": desired.inventory_path,
        "policyInventoryPath": desired.policy_inventory_path,
        "inventoryCommit": "a" * 40,
        "apiFloatingIp": "192.0.2.11",
        "ingressFloatingIp": "192.0.2.12",
    }


def legacy_receipt(desired):
    return {key: value for key, value in receipt(desired).items() if key in {
        "inventoryPath", "inventoryCommit", "apiFloatingIp", "ingressFloatingIp",
    }}


def result_pod(job, result, *, owner_uid=None, exit_code=0, container="provision"):
    return SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[
            SimpleNamespace(uid=owner_uid or job.metadata.uid),
        ]),
        status=SimpleNamespace(container_statuses=[SimpleNamespace(
            name=container,
            state=SimpleNamespace(terminated=SimpleNamespace(
                exit_code=exit_code, message=json.dumps(result),
            )),
        )]),
    )


def ready_status(desired, job):
    return {
        **{key: value for key, value in receipt(desired).items() if key != "schemaVersion"},
        "phase": "VirtualMachinesReady",
        "jobName": job.metadata.name,
        "observedGeneration": 4,
        "lastVerifiedAt": "2026-09-16T10:00:00Z",
        "conditions": [{
            "type": "Ready", "status": "True", "reason": "ProvisioningSucceeded",
            "lastTransitionTime": "2026-09-16T10:00:00Z",
        }],
    }


def assert_not_published(patch):
    assert patch.status["conditions"][0]["status"] == "False"
    assert PUBLICATION_STATUS_FIELDS.isdisjoint(patch.status)


def test_v015_golden_status_hash_upgrades_without_infrastructure_drift(
    monkeypatch, spec, profile, body, provisioning_input,
):
    apis = APIs(profile)
    patch = reconcile(monkeypatch, spec, body, apis, status={
        "phase": "VirtualMachinesReady",
        "inputHash": GOLDEN_V015_INPUT_HASH,
        "observedGeneration": body["metadata"]["generation"],
    })

    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert patch.status["inputHash"] == provisioning_input.input_hash == GOLDEN_V015_INPUT_HASH
    job = apis[2].create_namespaced_job.call_args.args[1]
    assert job.metadata.name == revision_job(body, provisioning_input).metadata.name
    assert job.metadata.name != revision_job(body, provisioning_input, legacy=True).metadata.name
    config_map = apis[1].create_namespaced_config_map.call_args.args[1]
    assert config_map.immutable is True
    assert json.loads(config_map.data["input.json"]) == provisioning_input.data
    assert_not_published(patch)


@pytest.mark.parametrize("completed_state", ["succeeded", "failed"])
def test_active_legacy_job_waits_then_creates_current_publication_job(
    monkeypatch, spec, profile, body, provisioning_input, completed_state,
):
    old = revision_job(body, provisioning_input, legacy=True, active=1)
    apis = APIs(profile, jobs=[old])
    patch = reconcile(monkeypatch, spec, body, apis, status={
        "inputHash": GOLDEN_V015_INPUT_HASH, "jobName": old.metadata.name,
    })
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    assert patch.status["jobName"] == old.metadata.name
    assert patch.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert_not_published(patch)
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()
    apis[1].list_namespaced_pod.assert_not_called()

    old.status.active = None
    setattr(old.status, completed_state, 1)
    old.status.conditions = [client.V1JobCondition(
        type="Failed" if completed_state == "failed" else "Complete", status="True",
    )]
    apis[1].list_namespaced_pod.return_value.items = [
        result_pod(old, legacy_receipt(provisioning_input)),
    ]
    following = reconcile(monkeypatch, spec, body, apis, status=dict(patch.status))
    assert following.status["jobName"] == revision_job(body, provisioning_input).metadata.name
    assert following.status["jobName"] != old.metadata.name
    assert following.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert following.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert_not_published(following)
    apis[1].create_namespaced_config_map.assert_called_once()
    apis[2].create_namespaced_job.assert_called_once()
    apis[1].list_namespaced_pod.assert_not_called()


def unfinished_status(state):
    if state == "absent":
        return None
    if state == "pending":
        return client.V1JobStatus(active=0, conditions=[])
    if state == "backoff":
        return client.V1JobStatus(active=0, failed=1, conditions=[])
    if state == "failed-false":
        return client.V1JobStatus(active=0, failed=1, conditions=[
            client.V1JobCondition(type="Failed", status="False"),
        ])
    if state == "failure-target":
        return client.V1JobStatus(active=0, failed=1, conditions=[
            client.V1JobCondition(type="FailureTarget", status="True"),
        ])
    if state == "running":
        return client.V1JobStatus(active=1, conditions=[])
    if state == "success-counter":
        return client.V1JobStatus(active=0, terminating=0, succeeded=1, conditions=[])
    if state == "success-active":
        return client.V1JobStatus(active=1, terminating=0, succeeded=1, conditions=[])
    if state == "success-terminating":
        return client.V1JobStatus(active=0, terminating=1, succeeded=1, conditions=[])
    if state == "complete-active":
        return client.V1JobStatus(active=1, terminating=0, succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ])
    if state == "complete-terminating":
        return client.V1JobStatus(active=0, terminating=1, succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ])
    if state == "failed-active":
        return client.V1JobStatus(active=1, terminating=0, failed=1, conditions=[
            client.V1JobCondition(type="Failed", status="True"),
        ])
    if state == "failed-terminating":
        return client.V1JobStatus(active=0, terminating=1, failed=1, conditions=[
            client.V1JobCondition(type="Failed", status="True"),
        ])
    raise ValueError(f"unknown test Job state {state}")


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "schema2"])
@pytest.mark.parametrize("state", UNFINISHED_STATES)
@pytest.mark.parametrize("terminal", ["Complete", "Failed"])
def test_nonterminal_previous_job_blocks_refresh_until_terminal(
    monkeypatch, spec, profile, body, provisioning_input, legacy, state, terminal,
):
    previous = revision_job(body, provisioning_input, legacy=legacy)
    previous.status = unfinished_status(state)
    old_map = input_config_map(
        name=previous.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    profile["ansible"]["nodeInterface"] = "enp1s0"
    apis = APIs(profile, profile_generation=2, jobs=[previous], pods=[])
    apis[1].list_namespaced_config_map.return_value.items = [old_map]
    status = {"inputHash": GOLDEN_V015_INPUT_HASH, "jobName": previous.metadata.name}

    waiting = reconcile(monkeypatch, spec, body, apis, status=status)

    assert waiting.status["phase"] == "ProvisioningInfrastructure"
    assert waiting.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    assert waiting.status["jobName"] == previous.metadata.name
    assert waiting.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert_not_published(waiting)
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()
    apis[1].delete_namespaced_config_map.assert_not_called()
    apis[2].delete_namespaced_job.assert_not_called()
    apis[1].list_namespaced_pod.assert_not_called()

    previous.status = client.V1JobStatus(
        active=0, succeeded=1 if terminal == "Complete" else None,
        failed=1 if terminal == "Failed" else None,
        conditions=[client.V1JobCondition(type=terminal, status="True")],
    )
    refreshed = reconcile(monkeypatch, spec, body, apis, status=status | dict(waiting.status))
    assert refreshed.status["phase"] == "ProvisioningInfrastructure"
    assert refreshed.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert refreshed.status["jobName"] != previous.metadata.name
    assert refreshed.status["jobName"].endswith("-g4-v7")
    assert refreshed.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert_not_published(refreshed)
    apis[1].create_namespaced_config_map.assert_called_once()
    apis[2].create_namespaced_job.assert_called_once()
    apis[1].list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("state", UNFINISHED_STATES)
def test_current_nonterminal_job_remains_provisioning_without_reading_pods(
    monkeypatch, spec, profile, body, provisioning_input, state,
):
    current = revision_job(body, provisioning_input)
    current.status = unfinished_status(state)
    apis = APIs(profile, jobs=[current], pods=[])

    patch = reconcile(monkeypatch, spec, body, apis, status={"inputHash": GOLDEN_V015_INPUT_HASH})

    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    assert patch.status["jobName"] == current.metadata.name
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("state", UNFINISHED_SUCCESS_STATES)
def test_successful_worker_receipt_waits_for_complete_and_zero_live_pods(
    monkeypatch, spec, profile, body, provisioning_input, state,
):
    current = revision_job(body, provisioning_input)
    current.status = unfinished_status(state)
    apis = APIs(profile, jobs=[current], pods=[result_pod(current, receipt(provisioning_input))])

    waiting = reconcile(monkeypatch, spec, body, apis, status={"inputHash": GOLDEN_V015_INPUT_HASH})

    assert waiting.status["phase"] == "ProvisioningInfrastructure"
    assert waiting.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    assert waiting.status["jobName"] == current.metadata.name
    assert_not_published(waiting)
    apis[1].list_namespaced_pod.assert_not_called()
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()

    current.status.active = 0
    current.status.terminating = 0
    current.status.conditions = [client.V1JobCondition(type="Complete", status="True")]
    ready = reconcile(monkeypatch, spec, body, apis, status=dict(waiting.status))
    assert ready.status["phase"] == "VirtualMachinesReady"
    assert ready.status["conditions"][0]["status"] == "True"
    assert ready.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert ready.status["publicationHash"] == provisioning_input.publication_hash
    apis[1].list_namespaced_pod.assert_called_once()
    apis[2].create_namespaced_job.assert_not_called()


def test_current_backoff_reports_inventory_conflict_only_after_terminal_failed_condition(
    monkeypatch, spec, profile, body, provisioning_input,
):
    current = revision_job(body, provisioning_input)
    current.status = unfinished_status("backoff")
    pod = result_pod(current, {"errorCode": "InventoryConflict"}, exit_code=1)
    apis = APIs(profile, jobs=[current], pods=[pod])
    waiting = reconcile(monkeypatch, spec, body, apis)
    assert waiting.status["phase"] == "ProvisioningInfrastructure"
    assert waiting.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    apis[1].list_namespaced_pod.assert_not_called()

    current.status.conditions = [client.V1JobCondition(type="Failed", status="True")]
    terminal = reconcile(monkeypatch, spec, body, apis, status=dict(waiting.status))
    assert terminal.status["phase"] == "Failed"
    assert terminal.status["conditions"][0]["reason"] == "InventoryConflict"
    assert_not_published(terminal)
    apis[1].list_namespaced_pod.assert_called_once()
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize(
    ("first_state", "second_state"),
    [
        ("pending", "backoff"), ("absent", "running"), ("backoff", "backoff"),
        ("success-active", "pending"), ("success-counter", "success-terminating"),
        ("complete-active", "complete-terminating"),
    ],
)
def test_multiple_unfinished_jobs_reject_reconciliation_even_without_active_pods(
    monkeypatch, spec, profile, body, provisioning_input, first_state, second_state,
):
    current = revision_job(body, provisioning_input)
    current.status = unfinished_status(first_state)
    previous = revision_job(body, provisioning_input, legacy=True)
    previous.status = unfinished_status(second_state)
    apis = APIs(profile, jobs=[previous, current], pods=[])

    patch = reconcile(monkeypatch, spec, body, apis)

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "multiple" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def test_successful_legacy_same_generation_and_bucket_backfills_both_inventories(
    monkeypatch, spec, profile, body, provisioning_input,
):
    old = revision_job(body, provisioning_input, legacy=True, succeeded=1)
    old_status = {
        **legacy_receipt(provisioning_input),
        "phase": "VirtualMachinesReady",
        "inputHash": GOLDEN_V015_INPUT_HASH,
        "jobName": old.metadata.name,
        "observedGeneration": body["metadata"]["generation"],
    }
    apis = APIs(profile, jobs=[old], pods=[result_pod(old, legacy_receipt(provisioning_input))])
    patch = reconcile(monkeypatch, spec, body, apis, status=old_status)
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()
    apis[2].create_namespaced_job.assert_called_once()
    current = apis[2].create_namespaced_job.call_args.args[1]
    assert current.metadata.name != old.metadata.name
    assert current.metadata.name.endswith("-g4-v7")
    current.metadata.uid = "backfill-job-uid"
    current.status = client.V1JobStatus(succeeded=1, conditions=[
        client.V1JobCondition(type="Complete", status="True"),
    ])
    apis[2].list_namespaced_job.return_value.items = [old, current]
    apis[1].list_namespaced_pod.return_value.items = [
        result_pod(old, legacy_receipt(provisioning_input)),
        result_pod(current, receipt(provisioning_input)),
    ]

    ready = reconcile(monkeypatch, spec, body, apis, status=old_status | dict(patch.status))
    assert ready.status["phase"] == "VirtualMachinesReady"
    assert ready.status["conditions"][0]["status"] == "True"
    assert ready.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    for key in PUBLICATION_STATUS_FIELDS - {"lastVerifiedAt"}:
        assert ready.status[key] == receipt(provisioning_input)[key]
    apis[2].create_namespaced_job.assert_called_once()


@pytest.mark.parametrize("change", ["interface", "python", "default-profile"])
def test_profile_only_revision_creates_distinct_immutable_job_same_generation_and_bucket(
    monkeypatch, spec, profile, body, provisioning_input, change,
):
    old = revision_job(body, provisioning_input, succeeded=1)
    old_config_map = input_config_map(
        name=old.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    old_source = old_config_map.data["input.json"]
    profile_uid = "profile-uid"
    profile_generation = 2
    if change == "interface":
        profile["ansible"]["nodeInterface"] = "enp1s0"
    elif change == "python":
        profile["ansible"]["pythonInterpreter"] = "/opt/python/bin/python3.13"
    else:
        assert "profileRef" not in spec
        assert provisioning_input.data["inventory"]["profileName"] == "standard-v1"
        monkeypatch.setattr(models, "DEFAULT_PROFILE", "reviewed-v2")
        profile_uid = "reviewed-v2-profile-uid"
        profile_generation = 1
    current = desired_input(
        spec, profile, body, profile_uid=profile_uid, profile_generation=profile_generation,
    )
    apis = APIs(
        profile, profile_uid=profile_uid, profile_generation=profile_generation,
        jobs=[old], pods=[result_pod(old, receipt(provisioning_input))],
    )
    apis[1].list_namespaced_config_map.return_value.items = [old_config_map]
    patch = reconcile(monkeypatch, spec, body, apis, status=ready_status(provisioning_input, old))

    assert current.input_hash == provisioning_input.input_hash == GOLDEN_V015_INPUT_HASH
    assert current.inventory_hash != provisioning_input.inventory_hash
    assert current.publication_hash != provisioning_input.publication_hash
    assert current.data == provisioning_input.data | {
        "inventory": current.data["inventory"],
        "profileRevision": {"uid": profile_uid, "generation": profile_generation},
    }
    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert patch.status["jobName"] == revision_job(body, current).metadata.name
    assert patch.status["jobName"] != old.metadata.name
    assert patch.status["jobName"].endswith("-g4-v7")
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()
    new_config_map = apis[1].create_namespaced_config_map.call_args.args[1]
    assert new_config_map.metadata.name != old_config_map.metadata.name
    assert new_config_map.immutable is True
    assert json.loads(new_config_map.data["input.json"]) == current.data
    assert old_config_map.data["input.json"] == old_source
    apis[1].delete_namespaced_config_map.assert_not_called()
    apis[2].delete_namespaced_job.assert_not_called()
    apis[0].get_cluster_custom_object.assert_called_once_with(
        "customer-clusters.sunet.se", "v1alpha1", "clusterprofiles",
        current.data["inventory"]["profileName"],
    )


def test_profile_a_b_a_revisions_create_three_jobs_without_reusing_old_ready_receipt(
    monkeypatch, spec, profile, body,
):
    apis = APIs(profile, profile_uid="profile-aba-uid")
    completed_jobs = []
    inputs = []
    config_maps = []
    status = {"inputHash": GOLDEN_V015_INPUT_HASH}
    for generation, interface in enumerate(("ens3", "enp1s0", "ens3"), start=1):
        profile["ansible"]["nodeInterface"] = interface
        apis[0].get_cluster_custom_object.return_value["metadata"]["generation"] = generation
        apis[2].list_namespaced_job.return_value.items = list(completed_jobs)
        apis[1].list_namespaced_config_map.return_value.items = list(config_maps)
        prior_pod_reads = apis[1].list_namespaced_pod.call_count

        created = reconcile(monkeypatch, spec, body, apis, status=status)

        assert created.status["phase"] == "ProvisioningInfrastructure"
        assert created.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
        assert_not_published(created)
        assert apis[1].list_namespaced_pod.call_count == prior_pod_reads
        assert apis[2].create_namespaced_job.call_count == generation
        current = apis[2].create_namespaced_job.call_args.args[1]
        assert current.metadata.name not in {job.metadata.name for job in completed_jobs}
        assert current.metadata.name.endswith("-g4-v7")
        config_map = apis[1].create_namespaced_config_map.call_args.args[1]
        assert config_map.immutable is True
        inputs.append(ProvisioningInput(json.loads(config_map.data["input.json"])))
        config_maps.append(config_map)
        current.metadata.uid = f"profile-revision-{generation}-job-uid"
        current.status = client.V1JobStatus(succeeded=1, conditions=[
            client.V1JobCondition(type="Complete", status="True"),
        ])
        completed_jobs.append(current)
        apis[2].list_namespaced_job.return_value.items = list(completed_jobs)
        apis[1].list_namespaced_pod.return_value.items = [
            result_pod(job, receipt(value))
            for job, value in zip(completed_jobs, inputs, strict=True)
        ]
        ready = reconcile(monkeypatch, spec, body, apis, status=status | dict(created.status))
        assert ready.status["phase"] == "VirtualMachinesReady"
        assert ready.status["conditions"][0]["status"] == "True"
        assert ready.status["publicationHash"] == inputs[-1].publication_hash
        assert ready.status["inputHash"] == GOLDEN_V015_INPUT_HASH
        status |= dict(created.status) | dict(ready.status)

    assert body["metadata"]["generation"] == 4
    assert [value.data["profileRevision"] for value in inputs] == [
        {"uid": "profile-aba-uid", "generation": generation} for generation in (1, 2, 3)
    ]
    assert len({job.metadata.name for job in completed_jobs}) == 3
    assert len({config_map.metadata.name for config_map in config_maps}) == 3
    assert len({value.publication_hash for value in inputs}) == 3
    assert {value.input_hash for value in inputs} == {GOLDEN_V015_INPUT_HASH}
    assert inputs[0].inventory_hash == inputs[2].inventory_hash != inputs[1].inventory_hash
    assert inputs[2].data == inputs[0].data | {"profileRevision": inputs[2].data["profileRevision"]}
    assert worker.render_cluster_policy(inputs[0].data) == (
        worker.render_cluster_policy(inputs[2].data)
    )


def test_recreated_profile_with_identical_policy_gets_new_job_without_infrastructure_drift(
    monkeypatch, spec, profile, body, provisioning_input,
):
    previous = revision_job(body, provisioning_input, succeeded=1)
    previous_map = input_config_map(
        name=previous.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    apis = APIs(
        profile, profile_uid="recreated-profile-uid", profile_generation=1, jobs=[previous],
        pods=[result_pod(previous, receipt(provisioning_input))],
    )
    apis[1].list_namespaced_config_map.return_value.items = [previous_map]
    created = reconcile(
        monkeypatch, spec, body, apis, status=ready_status(provisioning_input, previous),
    )

    assert created.status["phase"] == "ProvisioningInfrastructure"
    assert created.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert created.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert created.status["jobName"] != previous.metadata.name
    assert created.status["jobName"].endswith("-g4-v7")
    assert_not_published(created)
    apis[1].list_namespaced_pod.assert_not_called()
    config_map = apis[1].create_namespaced_config_map.call_args.args[1]
    current = ProvisioningInput(json.loads(config_map.data["input.json"]))
    assert config_map.immutable is True
    assert config_map.metadata.name != previous_map.metadata.name
    assert current.data["profileRevision"] == {"uid": "recreated-profile-uid", "generation": 1}
    assert current.data == provisioning_input.data | {
        "profileRevision": current.data["profileRevision"],
    }
    assert current.inventory_hash == provisioning_input.inventory_hash
    assert current.input_hash == provisioning_input.input_hash
    assert current.publication_hash != provisioning_input.publication_hash
    assert config_map.metadata.annotations[f"{ANNOTATION_PREFIX}publication-hash"] == (
        current.publication_hash
    )
    assert config_map.metadata.annotations[f"{ANNOTATION_PREFIX}inventory-input-hash"] == (
        provisioning_input.inventory_hash
    )


@pytest.mark.parametrize(
    ("profile_uid", "profile_generation"), [("profile-uid", 2), ("recreated-profile-uid", 1)],
)
def test_current_profile_revision_rejects_previous_receipt_with_identical_policy(
    monkeypatch, spec, profile, body, provisioning_input, profile_uid, profile_generation,
):
    current = desired_input(
        spec, profile, body, profile_uid=profile_uid, profile_generation=profile_generation,
    )
    assert current.input_hash == provisioning_input.input_hash == GOLDEN_V015_INPUT_HASH
    assert current.inventory_hash == provisioning_input.inventory_hash
    assert current.publication_hash != provisioning_input.publication_hash
    job = revision_job(body, current, succeeded=1)
    apis = APIs(
        profile, profile_uid=profile_uid, profile_generation=profile_generation,
        jobs=[job], pods=[result_pod(job, receipt(provisioning_input))],
    )

    patch = reconcile(monkeypatch, spec, body, apis, status={"inputHash": GOLDEN_V015_INPUT_HASH})

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "unexpected publicationHash" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("field", ["apiHostname", "argocdHostname", "profileName"])
def test_canonical_dns_or_selected_profile_changes_publish_without_resetting_infrastructure(
    monkeypatch, spec, profile, body, provisioning_input, field,
):
    old = revision_job(body, provisioning_input, succeeded=1)
    if field == "profileName":
        spec["profileRef"] = {"name": "reviewed-v2"}
    else:
        spec["dns"][field] = "changed.example.org"
    body["metadata"]["generation"] += 1
    body["spec"] = deepcopy(spec)
    current = desired_input(spec, profile, body)
    apis = APIs(profile, jobs=[old])
    patch = reconcile(monkeypatch, spec, body, apis, status=ready_status(provisioning_input, old))

    assert patch.status["phase"] == "ProvisioningInfrastructure"
    assert patch.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert patch.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert current.input_hash == provisioning_input.input_hash
    assert current.publication_hash != provisioning_input.publication_hash
    assert current.data == provisioning_input.data | {"inventory": current.data["inventory"]}
    assert patch.status["jobName"] == revision_job(body, current).metadata.name
    assert_not_published(patch)


@pytest.mark.parametrize("change", ["controller-flavor", "worker-flavor", "worker-groups"])
def test_real_infrastructure_changes_remain_blocked_by_existing_hash(
    monkeypatch, spec, profile, body, provisioning_input, change,
):
    old = revision_job(body, provisioning_input, succeeded=1)
    if change == "worker-groups":
        spec["workerGroups"] += 1
    else:
        profile["openstack"][change.removesuffix("-flavor")]["flavor"] = "b2.c16r32"
    changed = desired_input(spec, profile, body)
    assert changed.input_hash != GOLDEN_V015_INPUT_HASH
    apis = APIs(profile, jobs=[old])
    status = ready_status(provisioning_input, old)
    patch = reconcile(monkeypatch, spec, body, apis, status=status)

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InfrastructureDriftUnsupported"
    assert patch.status["conditions"][0]["status"] == "False"
    assert patch.status["inputHash"] == status["inputHash"] == GOLDEN_V015_INPUT_HASH
    assert patch.status["jobName"] == old.metadata.name
    apis[2].list_namespaced_job.assert_not_called()
    apis[1].create_namespaced_config_map.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("field", ANNOTATION_FIELDS)
@pytest.mark.parametrize("damage", ["missing", "wrong"])
@pytest.mark.parametrize("state", ["active", "succeeded", "failed"])
def test_expected_job_requires_every_current_input_annotation(
    monkeypatch, spec, profile, body, provisioning_input, field, damage, state,
):
    job = revision_job(body, provisioning_input, **{state: 1})
    key = f"{ANNOTATION_PREFIX}{field}"
    if damage == "missing":
        del job.metadata.annotations[key]
    else:
        job.metadata.annotations[key] = "stale-publication"
    apis = APIs(profile, jobs=[job], pods=[result_pod(job, receipt(provisioning_input))])
    patch = reconcile(monkeypatch, spec, body, apis)

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "current publication input" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def conflicting_apis(profile, config_map, job):
    apis = APIs(profile)
    apis[1].create_namespaced_config_map.side_effect = ApiException(status=409)
    apis[1].read_namespaced_config_map.side_effect = lambda name, namespace: (
        SimpleNamespace(data={"authorized_keys": "not-inspected"})
        if name == "cluster-authorized-keys" else config_map
    )
    apis[2].create_namespaced_job.side_effect = ApiException(status=409)
    apis[2].read_namespaced_job.return_value = job
    return apis


def test_repeated_reconcile_accepts_matching_configmap_and_job_409(
    monkeypatch, spec, profile, body, provisioning_input,
):
    job = revision_job(body, provisioning_input, active=1)
    config_map = input_config_map(
        name=job.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    apis = conflicting_apis(profile, config_map, job)
    status = {"inputHash": GOLDEN_V015_INPUT_HASH}
    for _ in range(2):
        patch = reconcile(monkeypatch, spec, body, apis, status=status)
        assert patch.status["phase"] == "ProvisioningInfrastructure"
        assert patch.status["jobName"] == job.metadata.name
        assert patch.status["inputHash"] == GOLDEN_V015_INPUT_HASH
        assert_not_published(patch)
        status |= dict(patch.status)
    assert apis[1].create_namespaced_config_map.call_count == 2
    assert apis[2].create_namespaced_job.call_count == 2
    assert apis[2].read_namespaced_job.call_count == 2
    assert config_map.immutable is True


@pytest.mark.parametrize("artifact", ["ConfigMap", "Job"])
@pytest.mark.parametrize("field", [*ANNOTATION_FIELDS, "owner"])
def test_409_rejects_mismatched_publication_annotations_or_owner(
    monkeypatch, spec, profile, body, provisioning_input, artifact, field,
):
    job = revision_job(body, provisioning_input, active=1)
    config_map = input_config_map(
        name=job.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    target = config_map if artifact == "ConfigMap" else job
    if field == "owner":
        target.metadata.owner_references[0].uid = "another-cluster"
    else:
        target.metadata.annotations[f"{ANNOTATION_PREFIX}{field}"] = "stale-publication"
    apis = conflicting_apis(profile, config_map, job)
    patch = reconcile(monkeypatch, spec, body, apis, status={"inputHash": GOLDEN_V015_INPUT_HASH})

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "conflicting provisioning" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    if artifact == "ConfigMap":
        apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("change", ["policy", "profile-uid", "profile-generation"])
def test_configmap_409_rejects_stale_source_even_with_matching_annotations(
    monkeypatch, spec, profile, body, provisioning_input, change,
):
    job = revision_job(body, provisioning_input, active=1)
    config_map = input_config_map(
        name=job.metadata.name, body=body, provisioning_input=provisioning_input,
    )
    stale = deepcopy(provisioning_input.data)
    if change == "policy":
        stale["inventory"]["nodeInterface"] = "enp1s0"
    elif change == "profile-uid":
        stale["profileRevision"]["uid"] = "previous-profile-uid"
    else:
        stale["profileRevision"]["generation"] += 1
    config_map.data["input.json"] = json.dumps(stale, sort_keys=True, separators=(",", ":"))
    apis = conflicting_apis(profile, config_map, job)
    patch = reconcile(monkeypatch, spec, body, apis)

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "conflicting provisioning input ConfigMap" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("field", ["inputHash", "inventoryInputHash", "publicationHash", "paths"])
def test_successful_receipt_must_match_current_desired_hashes_and_paths(
    monkeypatch, spec, profile, body, provisioning_input, field,
):
    job = revision_job(body, provisioning_input, succeeded=1)
    stale = receipt(provisioning_input)
    if field == "paths":
        stale["inventoryPath"] = "clusters/other/generated/ansible/hosts.yml"
        stale["policyInventoryPath"] = "inventory/clusters/other.yml"
    else:
        stale[field] = "e" * 64
    apis = APIs(profile, jobs=[job], pods=[result_pod(job, stale)])
    patch = reconcile(monkeypatch, spec, body, apis, status=ready_status(provisioning_input, job))

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert "unexpected" in patch.status["conditions"][0]["message"]
    assert_not_published(patch)
    apis[2].create_namespaced_job.assert_not_called()


@pytest.mark.parametrize("missing", [
    "schemaVersion", "inputHash", "inventoryInputHash", "publicationHash", "policyInventoryPath",
    "inventoryPath", "inventoryCommit", "apiFloatingIp", "ingressFloatingIp",
])
def test_successful_job_missing_receipt_proof_cannot_become_ready(
    monkeypatch, spec, profile, body, provisioning_input, missing,
):
    job = revision_job(body, provisioning_input, succeeded=1)
    incomplete = receipt(provisioning_input)
    del incomplete[missing]
    apis = APIs(profile, jobs=[job], pods=[result_pod(job, incomplete)])
    patch = reconcile(monkeypatch, spec, body, apis, status=ready_status(provisioning_input, job))

    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert_not_published(patch)


@pytest.mark.parametrize("other_result", ["legacy", "stale-publication"])
def test_mixed_successful_retry_results_cannot_become_ready(
    monkeypatch, spec, profile, body, provisioning_input, other_result,
):
    job = revision_job(body, provisioning_input, succeeded=1)
    current = receipt(provisioning_input)
    other = (
        legacy_receipt(provisioning_input) if other_result == "legacy"
        else current | {"publicationHash": "e" * 64}
    )
    apis = APIs(profile, jobs=[job], pods=[result_pod(job, current), result_pod(job, other)])
    patch = reconcile(monkeypatch, spec, body, apis, status=ready_status(provisioning_input, job))
    assert patch.status["phase"] == "Failed"
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert_not_published(patch)


@pytest.mark.parametrize("pod_kind", ["owned-worker", "foreign", "sidecar", "successful"])
def test_inventory_conflict_status_is_owner_checked_and_never_leaks_pod_content(
    monkeypatch, spec, profile, body, provisioning_input, pod_kind,
):
    job = revision_job(body, provisioning_input, failed=1)
    job.status.conditions = [client.V1JobCondition(
        type="Failed", status="True", reason="BackoffLimitExceeded", message="Worker failed",
    )]
    conflict = {
        "errorCode": "InventoryConflict",
        "message": "private-policy-content ansible_password=do-not-leak " + "x" * 1000,
    }
    pod = result_pod(
        job, conflict,
        owner_uid="foreign-job" if pod_kind == "foreign" else job.metadata.uid,
        exit_code=0 if pod_kind == "successful" else 1,
        container="sidecar" if pod_kind == "sidecar" else "provision",
    )
    apis = APIs(profile, jobs=[job], pods=[pod])
    patch = reconcile(monkeypatch, spec, body, apis)
    condition = patch.status["conditions"][0]
    assert patch.status["phase"] == "Failed"
    assert condition["reason"] == (
        "InventoryConflict" if pod_kind == "owned-worker" else "ProvisioningJobFailed"
    )
    if pod_kind == "owned-worker":
        assert condition["message"] == (
            "Inventory publication conflicts with existing policy or current desired state; "
            "review the worker logs without replacing retained infrastructure"
        )
    else:
        assert condition["message"] == "Worker failed"
    assert "private-policy-content" not in json.dumps(dict(patch.status))
    assert "do-not-leak" not in json.dumps(dict(patch.status))
    assert_not_published(patch)
    apis[1].read_namespaced_pod_log.assert_not_called()
    apis[2].create_namespaced_job.assert_not_called()


def test_conflict_code_from_unowned_job_is_not_trusted(
    monkeypatch, spec, profile, body, provisioning_input,
):
    job = revision_job(body, provisioning_input, failed=1)
    job.metadata.owner_references[0].uid = "another-cluster"
    apis = APIs(profile, jobs=[job], pods=[result_pod(
        job, {"errorCode": "InventoryConflict"}, exit_code=1,
    )])
    patch = reconcile(monkeypatch, spec, body, apis)
    assert patch.status["conditions"][0]["reason"] == "InvalidConfiguration"
    assert_not_published(patch)
    apis[1].list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize(
    ("profile_uid", "profile_generation"), [("profile-uid", 1), ("recreated-profile-uid", 3)],
)
def test_controller_configmap_worker_receipt_round_trip_publishes_pair_before_ready(
    monkeypatch, tmp_path, spec, profile, body, profile_uid, profile_generation,
):
    apis = APIs(profile, profile_uid=profile_uid, profile_generation=profile_generation)
    created = reconcile(monkeypatch, spec, body, apis)
    assert_not_published(created)
    config_map = apis[1].create_namespaced_config_map.call_args.args[1]
    job = apis[2].create_namespaced_job.call_args.args[1]
    path = tmp_path / "input.json"
    path.write_text(config_map.data["input.json"])
    data = worker.load_input(path)
    assert data["profileRevision"] == {"uid": profile_uid, "generation": profile_generation}
    resources = {
        "jumphost": {"name": "jump", "floating_ip": "192.0.2.10"},
        "controllers": [
            {"name": f"controller-{i}", "ip": f"10.44.0.{20 + i}"} for i in range(1, 4)
        ],
        "workers": [
            {"name": f"worker-{i}", "ip": f"10.44.0.{30 + i}"} for i in range(1, 7)
        ],
        "api_vip": "10.44.0.10", "ingress_vip": "10.44.0.11",
        "api_floating_ip": "192.0.2.11", "ingress_floating_ip": "192.0.2.12",
    }
    monkeypatch.setattr(worker, "read_public_keys", Mock(return_value=["ssh-ed25519 QUFBQQ=="]))
    monkeypatch.setattr(worker, "scoped_connection", Mock(return_value="fake-connection"))
    monkeypatch.setattr(worker, "Provisioner", Mock(return_value=Mock(
        provision=Mock(return_value=resources),
    )))
    published = Mock(return_value=("clusters/example/generated/ansible/hosts.yml", "b" * 40))
    monkeypatch.setattr(worker, "publish_inventory", published)
    result = worker.run(data, token="secret", clouds_file="/clouds")
    published.assert_called_once()
    assert published.call_args.kwargs["provisioning_data"] == data
    assert "customer_cluster_api_hostname:" in published.call_args.kwargs["cluster_policy"]
    assert "ProxyJump" in published.call_args.args[2]
    job.metadata.uid = "round-trip-job-uid"
    job.status = client.V1JobStatus(active=1, succeeded=1, conditions=[])
    apis[2].list_namespaced_job.return_value.items = [job]
    apis[1].list_namespaced_pod.return_value.items = [result_pod(job, result)]

    waiting = reconcile(monkeypatch, spec, body, apis, status=dict(created.status))
    assert waiting.status["phase"] == "ProvisioningInfrastructure"
    assert_not_published(waiting)
    apis[1].list_namespaced_pod.assert_not_called()

    job.status.active = 0
    job.status.conditions = [client.V1JobCondition(type="Complete", status="True")]
    ready = reconcile(monkeypatch, spec, body, apis, status=dict(waiting.status))
    assert ready.status["phase"] == "VirtualMachinesReady"
    assert ready.status["conditions"][0]["status"] == "True"
    assert ready.status["inputHash"] == GOLDEN_V015_INPUT_HASH
    for key in PUBLICATION_STATUS_FIELDS - {"lastVerifiedAt"}:
        assert ready.status[key] == result[key]
    assert ready.status["lastVerifiedAt"].endswith("Z")
    apis[1].create_namespaced_config_map.assert_called_once()
    apis[2].create_namespaced_job.assert_called_once()
