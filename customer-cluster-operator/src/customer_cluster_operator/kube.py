"""Kubernetes object validation and provisioning Job construction."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from .constants import INPUT_MOUNT, JOB_HISTORY_LIMIT, MANAGED_BY, MAX_MESSAGE, SSH_MOUNT
from .errors import ValidationError
from .models import ProvisioningInput


def bounded(message: object) -> str:
    text = " ".join(str(message).split())
    return text[:MAX_MESSAGE]


def condition(
    type_: str,
    status: str,
    reason: str,
    message: str = "",
    previous: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for item in previous or []:
        if item.get("type") == type_ and item.get("status") == status:
            timestamp = item.get("lastTransitionTime", timestamp)
            break
    return {
        "type": type_,
        "status": status,
        "reason": reason,
        "message": bounded(message),
        "lastTransitionTime": timestamp,
    }


def owner_reference(body: dict[str, Any]) -> client.V1OwnerReference:
    metadata = body["metadata"]
    return client.V1OwnerReference(
        api_version=body["apiVersion"],
        kind=body["kind"],
        name=metadata["name"],
        uid=metadata["uid"],
        controller=True,
        block_owner_deletion=False,
    )


def labels(uid: str, input_hash: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "customer-clusters.sunet.se/cluster-uid": uid,
        "customer-clusters.sunet.se/input-hash": input_hash[:63],
    }


def input_config_map(
    *, name: str, body: dict[str, Any], provisioning_input: ProvisioningInput
) -> client.V1ConfigMap:
    input_hash = provisioning_input.input_hash
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{name}-input",
            namespace=body["metadata"]["namespace"],
            labels=labels(body["metadata"]["uid"], input_hash),
            annotations={"customer-clusters.sunet.se/input-hash": input_hash},
            owner_references=[owner_reference(body)],
        ),
        immutable=True,
        data={"input.json": provisioning_input.canonical_json},
    )


def provisioning_job(
    *,
    name: str,
    body: dict[str, Any],
    provisioning_input: ProvisioningInput,
    worker_image: str,
    service_account: str,
) -> client.V1Job:
    data = provisioning_input.data
    namespace = body["metadata"]["namespace"]
    uid = body["metadata"]["uid"]
    input_hash = provisioning_input.input_hash
    cloud_ref = data["openstack"]["credentialsSecret"]
    ssh_ref = data["ssh"]["authorizedKeysConfigMap"]
    git_ref = data["git"]["tokenSecret"]
    pod_security = client.V1PodSecurityContext(
        run_as_non_root=True,
        run_as_user=1000,
        fs_group=1000,
        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
    )
    container = client.V1Container(
        name="provision",
        image=worker_image,
        image_pull_policy="IfNotPresent",
        command=["python", "-m", "customer_cluster_operator.worker"],
        env=[
            client.V1EnvVar(name="INPUT_FILE", value=f"{INPUT_MOUNT}/input.json"),
            client.V1EnvVar(name="OS_CLIENT_CONFIG_FILE", value="/etc/openstack/clouds.yaml"),
            client.V1EnvVar(name="HOME", value="/tmp"),  # noqa: S108
            client.V1EnvVar(name="XDG_CACHE_HOME", value="/tmp/.cache"),  # noqa: S108
            client.V1EnvVar(
                name="GIT_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=git_ref["name"], key=git_ref["key"]
                    )
                ),
            ),
        ],
        volume_mounts=[
            client.V1VolumeMount(name="input", mount_path=INPUT_MOUNT, read_only=True),
            client.V1VolumeMount(
                name="clouds",
                mount_path="/etc/openstack/clouds.yaml",
                sub_path="clouds.yaml",
                read_only=True,
            ),
            client.V1VolumeMount(name="ssh-keys", mount_path=SSH_MOUNT, read_only=True),
        ],
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            read_only_root_filesystem=True,
        ),
        resources=client.V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
    )
    volumes = [
        client.V1Volume(
            name="input",
            config_map=client.V1ConfigMapVolumeSource(name=f"{name}-input"),
        ),
        client.V1Volume(
            name="clouds",
            secret=client.V1SecretVolumeSource(
                secret_name=cloud_ref["name"],
                items=[client.V1KeyToPath(key=cloud_ref["key"], path="clouds.yaml")],
            ),
        ),
        client.V1Volume(
            name="ssh-keys",
            config_map=client.V1ConfigMapVolumeSource(name=ssh_ref["name"]),
        ),
        client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource()),
    ]
    container.volume_mounts.append(
        client.V1VolumeMount(name="tmp", mount_path="/tmp")  # noqa: S108
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels(uid, input_hash)),
        spec=client.V1PodSpec(
            restart_policy="Never",
            service_account_name=service_account,
            automount_service_account_token=False,
            security_context=pod_security,
            containers=[container],
            volumes=volumes,
        ),
    )
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels(uid, input_hash),
            owner_references=[owner_reference(body)],
            annotations={
                "customer-clusters.sunet.se/input-hash": input_hash,
                "customer-clusters.sunet.se/inventory-path": provisioning_input.inventory_path,
            },
        ),
        spec=client.V1JobSpec(
            template=template,
            backoff_limit=3,
            active_deadline_seconds=7200,
        ),
    )


def job_failure_message(job: client.V1Job) -> str:
    for item in job.status.conditions or []:
        if item.type == "Failed" and item.status == "True":
            return bounded(item.message or item.reason or "provisioning Job failed")
    return "provisioning Job failed"


def _job_finished(job: client.V1Job) -> bool:
    if job.status.succeeded:
        return True
    return any(
        item.type == "Failed" and item.status == "True" for item in job.status.conditions or []
    )


def cleanup_history(
    *,
    core_api: client.CoreV1Api,
    batch_api: client.BatchV1Api,
    namespace: str,
    cluster_uid: str,
    jobs: list[client.V1Job],
    current_job_name: str,
    status_job_name: str | None,
) -> list[client.V1Job]:
    """Bound owned Job, Pod, and input ConfigMap history without losing results."""
    if any(
        cluster_uid not in {ref.uid for ref in job.metadata.owner_references or []} for job in jobs
    ):
        raise ValidationError("refusing to clean up a Job not owned by this ManagedCluster")
    mandatory = {current_job_name}
    if status_job_name:
        mandatory.add(status_job_name)
    unfinished = {job.metadata.name for job in jobs if not _job_finished(job)}
    completed = sorted(
        (job for job in jobs if _job_finished(job)),
        key=lambda job: str(getattr(job.metadata, "creation_timestamp", None) or ""),
        reverse=True,
    )
    retained = mandatory | unfinished
    retained_completed = {job.metadata.name for job in completed if job.metadata.name in mandatory}
    for job in completed:
        name = job.metadata.name
        if name in retained_completed:
            continue
        if len(retained_completed) < JOB_HISTORY_LIMIT:
            retained.add(name)
            retained_completed.add(name)
            continue
        try:
            batch_api.delete_namespaced_job(
                name,
                namespace,
                propagation_policy="Background",
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    selector = f"customer-clusters.sunet.se/cluster-uid={cluster_uid}"
    config_maps = core_api.list_namespaced_config_map(namespace, label_selector=selector).items
    for config_map in config_maps:
        name = config_map.metadata.name
        owner_uids = {ref.uid for ref in config_map.metadata.owner_references or []}
        if cluster_uid not in owner_uids or not name.endswith("-input"):
            continue
        if name.removesuffix("-input") in retained:
            continue
        try:
            core_api.delete_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
    return [job for job in jobs if job.metadata.name in retained]


def job_result(core_api: client.CoreV1Api, namespace: str, job: client.V1Job) -> dict[str, str]:
    """Read and validate the owned worker container's termination message."""
    pods = core_api.list_namespaced_pod(
        namespace, label_selector=f"job-name={job.metadata.name}"
    ).items
    owned = [
        pod
        for pod in pods
        if any(ref.uid == job.metadata.uid for ref in pod.metadata.owner_references or [])
    ]
    successful = []
    for pod in owned:
        statuses = pod.status.container_statuses or []
        worker = next((item for item in statuses if item.name == "provision"), None)
        terminated = worker.state.terminated if worker and worker.state else None
        if terminated and terminated.exit_code == 0 and terminated.message:
            successful.append(terminated)
    if not successful:
        raise ValidationError("provisioning Job has no successful worker termination result")
    results = []
    for terminated in successful:
        try:
            result = json.loads(terminated.message)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("worker termination result is not valid JSON") from exc
        path = result.get("inventoryPath") if isinstance(result, dict) else None
        commit = result.get("inventoryCommit") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path.startswith("clusters/") or ".." in path.split("/"):
            raise ValidationError("worker termination result has an invalid inventoryPath")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValidationError("worker termination result has an invalid inventoryCommit")
        results.append((path, commit))
    if len(set(results)) != 1:
        raise ValidationError("provisioning Job Pods have conflicting successful results")
    path, commit = results[0]
    return {"inventoryPath": path, "inventoryCommit": commit}


def serialized(obj: object) -> str:
    """Stable representation useful in unit tests and diagnostics."""
    return json.dumps(client.ApiClient().sanitize_for_serialization(obj), sort_keys=True)
