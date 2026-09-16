"""Keep the customer-cluster package, build tag, and controller/worker pins aligned."""

import tomllib
from pathlib import Path

import pytest
import yaml

OPERATOR_ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.1.6"
DEPLOYMENT = (
    OPERATOR_ROOT.parents[2]
    / "k8s/platform-manifests/customer-cluster-operator/base/deployment.yaml"
)


def test_release_metadata_matches_jenkins_image_tag() -> None:
    project = tomllib.loads((OPERATOR_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    jenkins = yaml.safe_load((OPERATOR_ROOT.parent / ".jenkins.yaml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == RELEASE_VERSION
    jobs = [job for job in jenkins["extra_jobs"] if job["name"] == "customer-cluster-operator"]
    assert len(jobs) == 1
    assert jobs[0]["docker_name"] == "platform/customer-cluster-operator"
    assert jobs[0]["docker_context_dir"] == "customer-cluster-operator"
    assert jobs[0]["docker_tags"] == [RELEASE_VERSION, "latest"]
    readme = (OPERATOR_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Current package and image release: `{RELEASE_VERSION}`." in readme


def test_customer_cluster_release_does_not_bump_unrelated_images() -> None:
    jenkins = yaml.safe_load((OPERATOR_ROOT.parent / ".jenkins.yaml").read_text(encoding="utf-8"))
    assert jenkins["environment_variables"]["OPERATOR_VERSION"] == "0.1.7"
    assert jenkins["environment_variables"]["PORTAL_VERSION"] == "0.1.25"
    jobs = {job["name"]: job for job in jenkins["extra_jobs"]}
    assert jobs["openstack-operator"]["docker_tags"] == ["${OPERATOR_VERSION}", "latest"]
    assert jobs["customer-portal"]["docker_tags"] == ["${PORTAL_VERSION}", "latest"]


def test_controller_and_worker_images_match_release_when_checkout_is_available() -> None:
    if not DEPLOYMENT.is_file():
        pytest.skip("The sibling k8s/platform-manifests checkout is not present")

    deployment = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    controllers = [container for container in containers if container["name"] == "controller"]
    assert len(controllers) == 1
    image = f"docker.sunet.se/platform/customer-cluster-operator:{RELEASE_VERSION}"
    assert controllers[0]["image"] == image
    worker_images = [item for item in controllers[0]["env"] if item["name"] == "WORKER_IMAGE"]
    assert worker_images == [{"name": "WORKER_IMAGE", "value": image}]
