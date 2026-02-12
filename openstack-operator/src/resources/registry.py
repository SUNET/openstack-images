"""Managed resource registry backed by ConfigMap.

This module provides centralized tracking of all operator-managed OpenStack
resources via a Kubernetes ConfigMap. This enables reliable garbage collection
without depending on OpenStack tags or descriptions.
"""

import json
import logging
from typing import Any

import kubernetes
from kubernetes.client import ApiException, CoreV1Api, V1ConfigMap, V1ObjectMeta

logger = logging.getLogger(__name__)

CONFIGMAP_NAME = "openstack-operator-managed-resources"
CONFIGMAP_NAMESPACE = "openstack-operator"

# Resource type keys for the ConfigMap data
RESOURCE_TYPES = [
    "domains",
    "flavors",
    "images",
    "provider_networks",
    "projects",
    "groups",
    "networks",
    "security_groups",
    "federation_mappings",
]


class ResourceRegistry:
    """Registry for tracking operator-managed OpenStack resources.

    All managed resources are tracked in a single ConfigMap with separate
    JSON blobs for each resource type. This provides:
    - Uniform handling across all resource types
    - Reliable orphan detection for garbage collection
    - No dependency on OpenStack-side tagging
    """

    def __init__(self, k8s_api: CoreV1Api | None = None, namespace: str | None = None):
        """Initialize the registry.

        Args:
            k8s_api: Kubernetes CoreV1Api client. If None, will be created lazily.
            namespace: Namespace for the ConfigMap. Defaults to CONFIGMAP_NAMESPACE.
        """
        self._k8s_api = k8s_api
        self._namespace = namespace or CONFIGMAP_NAMESPACE

    @property
    def k8s_api(self) -> CoreV1Api:
        """Get or create the Kubernetes API client."""
        if self._k8s_api is None:
            kubernetes.config.load_incluster_config()
            self._k8s_api = CoreV1Api()
        return self._k8s_api

    def _get_configmap(self) -> dict[str, str]:
        """Get or create the tracking ConfigMap.

        Returns:
            The ConfigMap data dict.
        """
        try:
            cm = self.k8s_api.read_namespaced_config_map(
                CONFIGMAP_NAME, self._namespace
            )
            return cm.data or {}
        except ApiException as e:
            if e.status == 404:
                # Create empty ConfigMap
                logger.info(
                    "Creating managed resources ConfigMap: %s/%s",
                    self._namespace,
                    CONFIGMAP_NAME,
                )
                self.k8s_api.create_namespaced_config_map(
                    self._namespace,
                    V1ConfigMap(
                        metadata=V1ObjectMeta(name=CONFIGMAP_NAME),
                        data={},
                    ),
                )
                return {}
            raise

    def _update_configmap(self, data: dict[str, str]) -> None:
        """Update the ConfigMap with new data.

        Args:
            data: The new ConfigMap data dict.
        """
        self.k8s_api.patch_namespaced_config_map(
            CONFIGMAP_NAME,
            self._namespace,
            {"data": data},
        )

    def _get_resources(self, resource_type: str) -> dict[str, dict[str, Any]]:
        """Get resources of a specific type from the ConfigMap.

        Args:
            resource_type: The type of resources to get.

        Returns:
            Dict mapping resource names to their metadata.
        """
        data = self._get_configmap()
        key = f"{resource_type}.json"
        return json.loads(data.get(key, "{}"))

    def _set_resources(
        self, resource_type: str, resources: dict[str, dict[str, Any]]
    ) -> None:
        """Set resources of a specific type in the ConfigMap.

        Args:
            resource_type: The type of resources to set.
            resources: Dict mapping resource names to their metadata.
        """
        data = self._get_configmap()
        key = f"{resource_type}.json"
        data[key] = json.dumps(resources, sort_keys=True)
        self._update_configmap(data)

    def register(
        self,
        resource_type: str,
        name: str,
        resource_id: str,
        cr_name: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Register a managed resource.

        Args:
            resource_type: Type of resource (e.g., "projects", "networks").
            name: The OpenStack resource name.
            resource_id: The OpenStack resource ID.
            cr_name: The Kubernetes CR name that owns this resource.
            extra: Additional metadata to store.
        """
        resources = self._get_resources(resource_type)
        resources[name] = {
            "id": resource_id,
            "cr_name": cr_name,
            **(extra or {}),
        }
        self._set_resources(resource_type, resources)
        logger.debug(
            "Registered %s '%s' (id=%s, cr=%s)",
            resource_type,
            name,
            resource_id,
            cr_name,
        )

    def unregister(self, resource_type: str, name: str) -> None:
        """Remove a resource from the registry.

        Args:
            resource_type: Type of resource.
            name: The OpenStack resource name.
        """
        resources = self._get_resources(resource_type)
        if name in resources:
            resources.pop(name)
            self._set_resources(resource_type, resources)
            logger.debug("Unregistered %s '%s'", resource_type, name)

    def get(self, resource_type: str, name: str) -> dict[str, Any] | None:
        """Get metadata for a specific resource.

        Args:
            resource_type: Type of resource.
            name: The OpenStack resource name.

        Returns:
            Resource metadata dict or None if not found.
        """
        resources = self._get_resources(resource_type)
        return resources.get(name)

    def get_by_cr(self, resource_type: str, cr_name: str) -> list[dict[str, Any]]:
        """Get all resources owned by a specific CR.

        Args:
            resource_type: Type of resource.
            cr_name: The Kubernetes CR name.

        Returns:
            List of resource metadata dicts.
        """
        resources = self._get_resources(resource_type)
        return [
            {"name": name, **info}
            for name, info in resources.items()
            if info.get("cr_name") == cr_name
        ]

    def get_all(self, resource_type: str) -> dict[str, dict[str, Any]]:
        """Get all resources of a specific type.

        Args:
            resource_type: Type of resource.

        Returns:
            Dict mapping resource names to their metadata.
        """
        return self._get_resources(resource_type)

    def get_orphans(
        self, resource_type: str, expected_cr_names: set[str]
    ) -> list[dict[str, Any]]:
        """Find resources in registry that don't have corresponding CRs.

        Args:
            resource_type: Type of resource.
            expected_cr_names: Set of CR names that should exist.

        Returns:
            List of orphaned resource metadata dicts including their names.
        """
        resources = self._get_resources(resource_type)
        return [
            {"name": name, **info}
            for name, info in resources.items()
            if info.get("cr_name") not in expected_cr_names
        ]

    def list_all_cr_names(self, resource_type: str) -> set[str]:
        """List all CR names that have registered resources.

        Args:
            resource_type: Type of resource.

        Returns:
            Set of CR names.
        """
        resources = self._get_resources(resource_type)
        return {info.get("cr_name", "") for info in resources.values() if info.get("cr_name")}
