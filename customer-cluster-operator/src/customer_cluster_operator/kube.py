"""Kubernetes object validation and provisioning Job construction."""

from __future__ import annotations

import ipaddress
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


def labels(uid: str, input_hash: str, publication_hash: str | None = None) -> dict[str, str]:
    result = {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "customer-clusters.sunet.se/cluster-uid": uid,
        "customer-clusters.sunet.se/input-hash": input_hash[:63],
    }
    if publication_hash is not None:
        result["customer-clusters.sunet.se/publication-hash"] = publication_hash[:63]
    return result


def input_annotations(value: ProvisioningInput) -> dict[str, str]:
    return {
        "customer-clusters.sunet.se/input-hash": value.input_hash,
        "customer-clusters.sunet.se/publication-hash": value.publication_hash,
        "customer-clusters.sunet.se/inventory-input-hash": value.inventory_hash,
        "customer-clusters.sunet.se/inventory-path": value.inventory_path,
        "customer-clusters.sunet.se/policy-inventory-path": value.policy_inventory_path,
    }


def input_config_map(
    *, name: str, body: dict[str, Any], provisioning_input: ProvisioningInput
) -> client.V1ConfigMap:
    input_hash = provisioning_input.input_hash
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{name}-input",
            namespace=body["metadata"]["namespace"],
            labels=labels(body["metadata"]["uid"], input_hash, provisioning_input.publication_hash),
            annotations=input_annotations(provisioning_input),
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
        metadata=client.V1ObjectMeta(
            labels=labels(uid, input_hash, provisioning_input.publication_hash)
        ),
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
            labels=labels(uid, input_hash, provisioning_input.publication_hash),
            owner_references=[owner_reference(body)],
            annotations=input_annotations(provisioning_input),
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


def inventory_conflict(core_api: client.CoreV1Api, namespace: str, job: client.V1Job) -> bool:
    """Recognize a bounded failure code from an owned worker, never arbitrary pod log text."""
    uid = getattr(job.metadata, "uid", None)
    if not uid:
        return False
    pods = core_api.list_namespaced_pod(
        namespace, label_selector=f"job-name={job.metadata.name}"
    ).items
    for pod in pods:
        if not any(ref.uid == uid for ref in pod.metadata.owner_references or []):
            continue
        for status in pod.status.container_statuses or []:
            terminated = status.state.terminated if status.state else None
            if status.name != "provision" or not terminated or terminated.exit_code == 0:
                continue
            try:
                result = json.loads(terminated.message)
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and result.get("errorCode") == "InventoryConflict":
                return True
    return False


def job_finished(job: client.V1Job) -> bool:
    """A gap between worker Pods is not completion: pending/backoff Jobs still own publication."""
    if job.status is None:
        return False
    if job.status.active or getattr(job.status, "terminating", None):
        return False
    return any(
        item.type in {"Complete", "Failed"} and item.status == "True"
        for item in job.status.conditions or []
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
    unfinished = {job.metadata.name for job in jobs if not job_finished(job)}
    completed = sorted(
        (job for job in jobs if job_finished(job)),
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
        if (
            not isinstance(result, dict) or type(result.get("schemaVersion")) is not int
            or result["schemaVersion"] != 2
        ):
            raise ValidationError("worker result does not prove schema v2 inventory publication")
        path = result.get("inventoryPath")
        commit = result.get("inventoryCommit")
        matched = re.fullmatch(
            r"clusters/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)/generated/ansible/hosts[.]yml",
            path,
        ) if isinstance(path, str) else None
        if matched is None:
            raise ValidationError("worker termination result has an invalid inventoryPath")
        policy_path = result.get("policyInventoryPath")
        if policy_path != f"inventory/clusters/{matched[1]}.yml":
            raise ValidationError("worker termination result has an invalid policyInventoryPath")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValidationError("worker termination result has an invalid inventoryCommit")
        hashes = []
        for key in ("inputHash", "inventoryInputHash", "publicationHash"):
            value = result.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValidationError(f"worker termination result has an invalid {key}")
            hashes.append(value)
        endpoint_ips = []
        for key in ("apiFloatingIp", "ingressFloatingIp"):
            value = result.get(key) if isinstance(result, dict) else None
            try:
                if not isinstance(value, str):
                    raise ValueError()
                address = ipaddress.ip_address(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"worker termination result has an invalid {key}") from exc
            if address.version != 4:
                raise ValidationError(f"worker termination result has an invalid {key}")
            endpoint_ips.append(str(address))
        results.append((path, policy_path, commit, *hashes, *endpoint_ips))
    if len(set(results)) != 1:
        raise ValidationError("provisioning Job Pods have conflicting successful results")
    (
        path, policy_path, commit, input_hash, inventory_hash, publication_hash,
        api_fip, ingress_fip,
    ) = results[0]
    return {
        "inventoryPath": path,
        "inventoryCommit": commit,
        "policyInventoryPath": policy_path,
        "inputHash": input_hash,
        "inventoryInputHash": inventory_hash,
        "publicationHash": publication_hash,
        "apiFloatingIp": api_fip,
        "ingressFloatingIp": ingress_fip,
    }


def serialized(obj: object) -> str:
    """Stable representation useful in unit tests and diagnostics."""
    return json.dumps(client.ApiClient().sanitize_for_serialization(obj), sort_keys=True)
