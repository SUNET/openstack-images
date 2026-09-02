"""Kopf reconciliation for ManagedCluster infrastructure Jobs."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import kopf
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from .config import Settings
from .constants import (
    API_GROUP,
    API_VERSION,
    CLUSTER_PLURAL,
    PROFILE_PLURAL,
    PROJECT_GROUP,
    PROJECT_PLURAL,
    PROJECT_VERSION,
)
from .errors import ValidationError
from .kube import (
    cleanup_history,
    condition,
    input_config_map,
    job_failure_message,
    job_result,
    labels,
    provisioning_job,
)
from .models import build_input, is_suspended, job_name, profile_name

LOG = logging.getLogger(__name__)
_apis: tuple[client.CustomObjectsApi, client.CoreV1Api, client.BatchV1Api] | None = None


def get_apis() -> tuple[client.CustomObjectsApi, client.CoreV1Api, client.BatchV1Api]:
    global _apis
    if _apis is None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _apis = (client.CustomObjectsApi(), client.CoreV1Api(), client.BatchV1Api())
    return _apis


def _set_status(
    patch: kopf.Patch,
    *,
    phase: str,
    generation: int,
    ready: str,
    reason: str,
    message: str = "",
    job: str | None = None,
    input_hash: str | None = None,
    inventory_path: str | None = None,
    inventory_commit: str | None = None,
    last_verified_at: str | None = None,
    existing_status: dict[str, Any] | None = None,
) -> None:
    patch.status["phase"] = phase
    patch.status["observedGeneration"] = generation
    patch.status["conditions"] = [
        condition(
            "Ready",
            ready,
            reason,
            message,
            (existing_status or {}).get("conditions"),
        )
    ]
    if job is not None:
        patch.status["jobName"] = job
    if input_hash is not None:
        patch.status["inputHash"] = input_hash
    if inventory_path is not None:
        patch.status["inventoryPath"] = inventory_path
    if inventory_commit is not None:
        patch.status["inventoryCommit"] = inventory_commit
    if last_verified_at is not None:
        patch.status["lastVerifiedAt"] = last_verified_at


def _read_profile(api: client.CustomObjectsApi, name: str) -> dict[str, Any]:
    try:
        return api.get_cluster_custom_object(API_GROUP, API_VERSION, PROFILE_PLURAL, name)
    except ApiException as exc:
        if exc.status == 404:
            raise ValidationError(f"ClusterProfile {name} does not exist") from exc
        raise


def _read_project(api: client.CustomObjectsApi, namespace: str, name: str) -> dict[str, Any] | None:
    try:
        return api.get_namespaced_custom_object(
            PROJECT_GROUP, PROJECT_VERSION, namespace, PROJECT_PLURAL, name
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _project_ready(project: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(project, dict):
        return False, None
    metadata = project.get("metadata")
    status = project.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        return False, None
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    project_id = status.get("projectId")
    valid_generations = (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
        and isinstance(observed_generation, int)
        and not isinstance(observed_generation, bool)
    )
    ready = (
        valid_generations
        and observed_generation == generation
        and status.get("phase") == "Ready"
        and isinstance(project_id, str)
        and bool(project_id.strip())
    )
    return ready, project_id.strip() if ready else None


def _prerequisites_ready(core_api: client.CoreV1Api, desired: Any) -> bool:
    refs = desired.data
    secrets = [
        refs["openstack"]["credentialsSecret"],
        refs["git"]["tokenSecret"],
    ]
    try:
        for ref in secrets:
            secret = core_api.read_namespaced_secret(ref["name"], ref["namespace"])
            if ref["key"] not in (secret.data or {}):
                return False
        ref = refs["ssh"]["authorizedKeysConfigMap"]
        config_map = core_api.read_namespaced_config_map(ref["name"], ref["namespace"])
        return ref["key"] in (config_map.data or {})
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


def _find_jobs(batch_api: client.BatchV1Api, namespace: str, uid: str) -> list[client.V1Job]:
    selector = f"customer-clusters.sunet.se/cluster-uid={uid}"
    return list(batch_api.list_namespaced_job(namespace, label_selector=selector).items)


def _job_owned(job: client.V1Job, uid: str) -> bool:
    return any(ref.uid == uid for ref in job.metadata.owner_references or [])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _verification_bucket(settings: Settings) -> int:
    return int(_utcnow().timestamp()) // settings.verification_interval


def reconcile(
    *,
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    body: dict[str, Any],
    namespace: str,
    name: str,
    **_: Any,
) -> None:
    """Validate desired state, create one Job, and project Job state to status."""
    generation = int(body.get("metadata", {}).get("generation", 1))
    try:
        settings = Settings.from_env()
        if namespace != settings.operator_namespace:
            raise ValidationError(
                f"ManagedCluster must be in namespace {settings.operator_namespace}"
            )
        if is_suspended(spec):
            _set_status(
                patch,
                phase="Suspended",
                generation=generation,
                ready="False",
                reason="ProvisioningSuspended",
                message="Provisioning is suspended; retained resources are unchanged",
                existing_status=status,
            )
            return
        custom_api, core_api, batch_api = get_apis()
        selected_profile = _read_profile(custom_api, profile_name(spec))
        profile_spec = selected_profile.get("spec")
        if not isinstance(profile_spec, dict):
            raise ValidationError("ClusterProfile spec must be an object")
        project_namespace = profile_spec.get("projectNamespace")
        if not isinstance(project_namespace, str) or not project_namespace:
            raise ValidationError("profile.spec.projectNamespace must be a non-empty string")
        os_spec = spec.get("openstack")
        if not isinstance(os_spec, dict) or not os_spec.get("projectResourceName"):
            raise ValidationError("spec.openstack.projectResourceName is required")
        project = _read_project(custom_api, project_namespace, os_spec["projectResourceName"])
        project_ready, project_id = _project_ready(project)
        if not project_ready:
            _set_status(
                patch,
                phase="PendingProject",
                generation=generation,
                ready="False",
                reason="ProjectNotReady",
                message=(
                    "Referenced OpenstackProject is not Ready with a projectId at its current "
                    "generation"
                ),
                existing_status=status,
            )
            return
        project_spec = (project or {}).get("spec") or {}
        if project_spec.get("name") != os_spec.get("projectName"):
            raise ValidationError(
                "spec.openstack.projectName does not match the referenced OpenstackProject"
            )
        if project_spec.get("contractNumber") != spec.get("contractNumber"):
            raise ValidationError(
                "spec.contractNumber does not match the referenced OpenstackProject"
            )
        if project_spec.get("managed") is not True:
            raise ValidationError("referenced OpenstackProject must have spec.managed true")

        desired = build_input(
            spec=spec,
            profile=profile_spec,
            uid=body["metadata"]["uid"],
            slug=name,
            namespace=namespace,
            project_id=project_id,
            operator_namespace=settings.operator_namespace,
        )
        desired_hash = desired.input_hash
        existing_hash = status.get("inputHash")
        if existing_hash and existing_hash != desired_hash:
            _set_status(
                patch,
                phase="Failed",
                generation=generation,
                ready="False",
                reason="InfrastructureDriftUnsupported",
                message="Infrastructure spec or ClusterProfile changed after provisioning started",
                job=status.get("jobName"),
                input_hash=existing_hash,
                inventory_path=status.get("inventoryPath"),
                inventory_commit=status.get("inventoryCommit"),
                last_verified_at=status.get("lastVerifiedAt"),
                existing_status=status,
            )
            return

        bucket = _verification_bucket(settings)
        expected_job = job_name(name, body["metadata"]["uid"], desired_hash, generation, bucket)
        jobs = _find_jobs(batch_api, namespace, body["metadata"]["uid"])
        if any(not _job_owned(job, body["metadata"]["uid"]) for job in jobs):
            raise ValidationError("found provisioning Job not owned by this ManagedCluster")
        jobs = cleanup_history(
            core_api=core_api,
            batch_api=batch_api,
            namespace=namespace,
            cluster_uid=body["metadata"]["uid"],
            jobs=jobs,
            current_job_name=expected_job,
            status_job_name=status.get("jobName"),
        )
        active = [job for job in jobs if job.status.active]
        if len(active) > 1:
            raise ValidationError("multiple provisioning Jobs are active for this ManagedCluster")
        if active and active[0].metadata.name != expected_job:
            active_hash = (getattr(active[0].metadata, "annotations", None) or {}).get(
                "customer-clusters.sunet.se/input-hash"
            )
            if active_hash != desired_hash:
                raise ValidationError("another provisioning Job is active for this ManagedCluster")
            _set_status(
                patch,
                phase="ProvisioningInfrastructure",
                generation=generation,
                ready="False",
                reason="ProvisioningJobRunning",
                message="Waiting for the previous-generation idempotent Job",
                job=active[0].metadata.name,
                input_hash=desired_hash,
                inventory_path=desired.inventory_path,
                existing_status=status,
            )
            return

        existing_job = next((job for job in jobs if job.metadata.name == expected_job), None)
        if existing_job is not None:
            if existing_job.status.succeeded:
                result = job_result(core_api, namespace, existing_job)
                if result["inventoryPath"] != desired.inventory_path:
                    raise ValidationError("worker returned an unexpected inventoryPath")
                _set_status(
                    patch,
                    phase="VirtualMachinesReady",
                    generation=generation,
                    ready="True",
                    reason="ProvisioningSucceeded",
                    message="OpenStack resources and generated inventory are ready",
                    job=expected_job,
                    input_hash=desired_hash,
                    inventory_path=result["inventoryPath"],
                    inventory_commit=result["inventoryCommit"],
                    last_verified_at=_utcnow().isoformat().replace("+00:00", "Z"),
                    existing_status=status,
                )
            elif existing_job.status.failed and not existing_job.status.active:
                _set_status(
                    patch,
                    phase="Failed",
                    generation=generation,
                    ready="False",
                    reason="ProvisioningJobFailed",
                    message=job_failure_message(existing_job),
                    job=expected_job,
                    input_hash=desired_hash,
                    inventory_path=desired.inventory_path,
                    existing_status=status,
                )
            else:
                _set_status(
                    patch,
                    phase="ProvisioningInfrastructure",
                    generation=generation,
                    ready="False",
                    reason="ProvisioningJobRunning",
                    job=expected_job,
                    input_hash=desired_hash,
                    inventory_path=desired.inventory_path,
                    existing_status=status,
                )
            return

        if not _prerequisites_ready(core_api, desired):
            _set_status(
                patch,
                phase="PendingPrerequisites",
                generation=generation,
                ready="False",
                reason="PrerequisitesNotReady",
                message="A referenced Secret, ConfigMap, or required key is missing",
                existing_status=status,
            )
            return

        config_map = input_config_map(name=expected_job, body=body, provisioning_input=desired)
        try:
            core_api.create_namespaced_config_map(namespace, config_map)
        except ApiException as exc:
            if exc.status != 409:
                raise
            existing = core_api.read_namespaced_config_map(config_map.metadata.name, namespace)
            owner_uids = {item.uid for item in existing.metadata.owner_references or []}
            if existing.data != config_map.data or body["metadata"]["uid"] not in owner_uids:
                raise ValidationError("conflicting provisioning input ConfigMap exists") from exc
        job = provisioning_job(
            name=expected_job,
            body=body,
            provisioning_input=desired,
            worker_image=settings.worker_image,
            service_account=settings.worker_service_account,
        )
        try:
            batch_api.create_namespaced_job(namespace, job)
        except ApiException as exc:
            if exc.status != 409:
                raise
            existing = batch_api.read_namespaced_job(expected_job, namespace)
            if existing.metadata.labels != labels(
                body["metadata"]["uid"], desired_hash
            ) or not _job_owned(existing, body["metadata"]["uid"]):
                raise ValidationError("conflicting provisioning Job exists") from exc
        _set_status(
            patch,
            phase="ProvisioningInfrastructure",
            generation=generation,
            ready="False",
            reason="ProvisioningJobCreated",
            job=expected_job,
            input_hash=desired_hash,
            inventory_path=desired.inventory_path,
            existing_status=status,
        )
    except ValidationError as exc:
        _set_status(
            patch,
            phase="Failed",
            generation=generation,
            ready="False",
            reason="InvalidConfiguration",
            message=str(exc),
            existing_status=status,
        )
    except ApiException:
        raise
    except Exception as exc:
        LOG.exception("ManagedCluster reconciliation failed for %s/%s", namespace, name)
        _set_status(
            patch,
            phase="Failed",
            generation=generation,
            ready="False",
            reason="ReconciliationError",
            message=str(exc),
            job=status.get("jobName"),
            input_hash=status.get("inputHash"),
            inventory_path=status.get("inventoryPath"),
            inventory_commit=status.get("inventoryCommit"),
            last_verified_at=status.get("lastVerifiedAt"),
            existing_status=status,
        )


@kopf.on.create(API_GROUP, API_VERSION, CLUSTER_PLURAL)
@kopf.on.resume(API_GROUP, API_VERSION, CLUSTER_PLURAL)
@kopf.on.update(API_GROUP, API_VERSION, CLUSTER_PLURAL)
def on_change(**kwargs: Any) -> None:
    reconcile(**kwargs)


@kopf.timer(API_GROUP, API_VERSION, CLUSTER_PLURAL, interval=30.0, sharp=True)
def on_timer(**kwargs: Any) -> None:
    reconcile(**kwargs)


@kopf.on.delete(API_GROUP, API_VERSION, CLUSTER_PLURAL, optional=True)
def on_delete(name: str, namespace: str, **_: Any) -> None:
    """Intentionally retain all external OpenStack and Git resources."""
    LOG.info("Retaining external resources for deleted ManagedCluster %s/%s", namespace, name)


def main() -> None:
    settings = Settings.from_env()
    os.execvp(  # noqa: S606 - fixed executable and arguments
        "kopf",
        [
            "kopf",
            "run",
            "--standalone",
            "--liveness=http://0.0.0.0:8080/healthz",
            "--namespace",
            settings.operator_namespace,
            "--module",
            "customer_cluster_operator.controller",
        ],
    )
