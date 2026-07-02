"""Kubernetes client for reading OpenstackProject CR status."""

import logging

from kubernetes import client, config

logger = logging.getLogger(__name__)

_api: client.CustomObjectsApi | None = None


def init_k8s() -> None:
    """Initialize the Kubernetes client (in-cluster or kubeconfig)."""
    global _api
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _api = client.CustomObjectsApi()


def get_project_status(resource_name: str, namespace: str = "customer-projects") -> dict | None:
    """Get the status of an OpenstackProject CR.

    Returns a dict with phase, projectId, conditions, etc., or None if not found.
    """
    if _api is None:
        return None

    try:
        cr = _api.get_namespaced_custom_object(
            group="sunet.se",
            version="v1alpha1",
            namespace=namespace,
            plural="openstackprojects",
            name=resource_name,
        )
        return cr.get("status", {})
    except client.ApiException as e:
        if e.status == 404:
            return None
        logger.warning("Failed to get OpenstackProject %s: %s", resource_name, e)
        return None


def find_project_cr_by_spec_name(spec_name: str) -> str | None:
    """Find any OpenstackProject CR (cluster-wide) targeting an OpenStack project.

    Catches projects defined outside the portal's git repo, e.g. CRs applied
    directly through ArgoCD, so the portal cannot create a duplicate that
    would fight over the same Keystone project.

    Returns 'namespace/name' of the CR, or None if no CR matches (or the
    lookup fails — the operator has its own duplicate guard as backstop).
    """
    if _api is None:
        return None

    try:
        crs = _api.list_cluster_custom_object(
            group="sunet.se",
            version="v1alpha1",
            plural="openstackprojects",
        )
    except client.ApiException as e:
        logger.warning("Failed to list OpenstackProjects: %s", e)
        return None

    for cr in crs.get("items", []):
        if cr.get("spec", {}).get("name") == spec_name:
            meta = cr.get("metadata", {})
            return f"{meta.get('namespace', '')}/{meta.get('name', '')}"
    return None
