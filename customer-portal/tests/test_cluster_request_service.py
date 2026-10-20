"""Unit tests for managed-cluster request publication ordering."""

import json
from types import SimpleNamespace

import pytest

from app.cluster_quotas import managed_cluster_quotas
from app.cluster_request_service import _apply_resize


class ProjectBackend:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.quotas = None

    def update_project(self, *, resource_name: str, quotas: dict) -> None:
        assert resource_name == "cluster-example"
        self.quotas = quotas
        self.events.append("project-quota")


class ClusterBackend:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.worker_groups = None

    def update_worker_groups(self, slug: str, worker_groups: int) -> None:
        assert slug == "cluster"
        if self.fail:
            raise RuntimeError("publication failed")
        self.worker_groups = worker_groups
        self.events.append("cluster-resize")


def resize_state() -> tuple[SimpleNamespace, SimpleNamespace, dict]:
    cluster = SimpleNamespace(
        slug="cluster",
        worker_groups=1,
        management_project_resource_name="cluster-example",
    )
    request = SimpleNamespace(payload=json.dumps({"target_worker_groups": 3}))
    payload = {"target_worker_groups": 3}
    return cluster, request, payload


@pytest.mark.asyncio
async def test_resize_grants_quota_before_publishing_cluster_capacity() -> None:
    events: list[str] = []
    projects = ProjectBackend(events)
    clusters = ClusterBackend(events)
    cluster, request, payload = resize_state()

    await _apply_resize(cluster, request, payload, None, projects, clusters)

    assert events == ["project-quota", "cluster-resize"]
    assert projects.quotas == managed_cluster_quotas(3)
    assert clusters.worker_groups == 3
    assert cluster.worker_groups == 3
    assert json.loads(request.payload) == {
        "target_worker_groups": 3,
        "before_worker_groups": 1,
    }


@pytest.mark.asyncio
async def test_resize_publication_failure_preserves_portal_state() -> None:
    events: list[str] = []
    projects = ProjectBackend(events)
    clusters = ClusterBackend(events, fail=True)
    cluster, request, payload = resize_state()
    original_payload = request.payload

    with pytest.raises(RuntimeError, match="publication failed"):
        await _apply_resize(cluster, request, payload, None, projects, clusters)

    assert events == ["project-quota"]
    assert projects.quotas == managed_cluster_quotas(3)
    assert cluster.worker_groups == 1
    assert request.payload == original_payload
