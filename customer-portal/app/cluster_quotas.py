"""Quota calculations for managed Kubernetes cluster projects."""

from copy import deepcopy

from app.git_backend import DEFAULT_QUOTAS


def managed_cluster_quotas(worker_groups: int) -> dict:
    """Return OpenStack quotas sized for a managed cluster profile."""
    if worker_groups < 1:
        raise ValueError("worker_groups must be at least 1")

    instances = 4 + 3 * worker_groups
    quotas = deepcopy(DEFAULT_QUOTAS)
    quotas["compute"] = {
        "instances": instances,
        "cores": 7 + 12 * worker_groups,
        "ramMB": (14 + 48 * worker_groups) * 1024,
    }
    quotas["storage"].update(
        {
            "volumes": instances,
            "volumesGB": 320 + 300 * worker_groups,
        }
    )
    quotas["network"].update(
        {
            "floatingIps": max(3, quotas["network"].get("floatingIps", 0)),
            "networks": max(1, quotas["network"].get("networks", 0)),
            "subnets": max(1, quotas["network"].get("subnets", 0)),
            "routers": max(1, quotas["network"].get("routers", 0)),
            "ports": max(2 * instances, quotas["network"].get("ports", 0)),
        }
    )
    return quotas
