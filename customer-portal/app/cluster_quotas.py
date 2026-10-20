"""Quota calculations for managed Kubernetes cluster projects."""

from copy import deepcopy

from app.git_backend import DEFAULT_QUOTAS

# standard-v1 fixed footprint: three controllers and one jumphost.
_FIXED_INSTANCES = 4
_FIXED_CORES = 7
_FIXED_RAM_GIB = 14
_FIXED_STORAGE_GIB = 320

# standard-v1 worker groups contain three b2.c4r16 workers with 100 GiB disks.
_WORKER_GROUP_INSTANCES = 3
_WORKER_GROUP_CORES = 12
_WORKER_GROUP_RAM_GIB = 48
_WORKER_GROUP_STORAGE_GIB = 300

# Operational reserve: one worker group and one b2.c2r4 controller replacement.
_RESERVE_INSTANCES = 4
_RESERVE_CORES = 14
_RESERVE_RAM_GIB = 52
_RESERVE_STORAGE_GIB = 400
_MIN_STORAGE_GIB = 1024


def managed_cluster_quotas(worker_groups: int) -> dict:
    """Return OpenStack quotas sized for a managed cluster profile.

    Quotas include enough operational headroom for one complete worker group
    and one controller replacement. The storage quota is never below 1 TiB.
    """
    if worker_groups < 1:
        raise ValueError("worker_groups must be at least 1")

    instances = (
        _FIXED_INSTANCES
        + _WORKER_GROUP_INSTANCES * worker_groups
        + _RESERVE_INSTANCES
    )
    quotas = deepcopy(DEFAULT_QUOTAS)
    quotas["compute"] = {
        "instances": instances,
        "cores": _FIXED_CORES + _WORKER_GROUP_CORES * worker_groups + _RESERVE_CORES,
        "ramMB": (
            _FIXED_RAM_GIB + _WORKER_GROUP_RAM_GIB * worker_groups + _RESERVE_RAM_GIB
        )
        * 1024,
    }
    quotas["storage"].update(
        {
            "volumes": instances,
            "volumesGB": max(
                _MIN_STORAGE_GIB,
                _FIXED_STORAGE_GIB
                + _WORKER_GROUP_STORAGE_GIB * worker_groups
                + _RESERVE_STORAGE_GIB,
            ),
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
