"""Shared operator state - thread-safe singleton for OpenStack and Kubernetes clients."""

import threading
from dataclasses import dataclass, field

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from openstack_client import OpenStackClient
from resources.registry import ResourceRegistry


@dataclass
class OperatorState:
    """Thread-safe operator state container.

    This class provides thread-safe access to shared operator resources:
    - OpenStack client
    - Kubernetes API clients
    - Resource registry

    All handlers should use the global `state` instance rather than
    creating their own clients.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _os_client: OpenStackClient | None = field(default=None, repr=False)
    _registry: ResourceRegistry | None = field(default=None, repr=False)
    _k8s_core_api: k8s_client.CoreV1Api | None = field(default=None, repr=False)
    _k8s_custom_api: k8s_client.CustomObjectsApi | None = field(default=None, repr=False)
    _k8s_configured: bool = field(default=False, repr=False)

    def _ensure_k8s_config(self) -> None:
        """Ensure Kubernetes configuration is loaded (must hold lock)."""
        if not self._k8s_configured:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            self._k8s_configured = True

    def get_openstack_client(self) -> OpenStackClient:
        """Get or create the OpenStack client (thread-safe)."""
        with self._lock:
            if self._os_client is None:
                self._os_client = OpenStackClient()
            return self._os_client

    def get_registry(self) -> ResourceRegistry:
        """Get or create the resource registry (thread-safe)."""
        with self._lock:
            if self._registry is None:
                self._registry = ResourceRegistry()
            return self._registry

    def get_k8s_core_api(self) -> k8s_client.CoreV1Api:
        """Get or create the Kubernetes CoreV1Api client (thread-safe)."""
        with self._lock:
            self._ensure_k8s_config()
            if self._k8s_core_api is None:
                self._k8s_core_api = k8s_client.CoreV1Api()
            return self._k8s_core_api

    def get_k8s_custom_api(self) -> k8s_client.CustomObjectsApi:
        """Get or create the Kubernetes CustomObjectsApi client (thread-safe)."""
        with self._lock:
            self._ensure_k8s_config()
            if self._k8s_custom_api is None:
                self._k8s_custom_api = k8s_client.CustomObjectsApi()
            return self._k8s_custom_api

    def close(self) -> None:
        """Close all connections."""
        with self._lock:
            if self._os_client is not None:
                self._os_client.close()
                self._os_client = None


# Global operator state singleton
state = OperatorState()


# Convenience functions for backwards compatibility
def get_openstack_client() -> OpenStackClient:
    """Get the shared OpenStack client."""
    return state.get_openstack_client()


def get_registry() -> ResourceRegistry:
    """Get the shared resource registry."""
    return state.get_registry()


def get_k8s_core_api() -> k8s_client.CoreV1Api:
    """Get the shared Kubernetes CoreV1Api client."""
    return state.get_k8s_core_api()


def get_k8s_custom_api() -> k8s_client.CustomObjectsApi:
    """Get the shared Kubernetes CustomObjectsApi client."""
    return state.get_k8s_custom_api()
