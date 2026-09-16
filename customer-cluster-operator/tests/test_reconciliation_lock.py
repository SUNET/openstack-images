"""Deterministic timer/change serialization and weak lock lifetime regressions."""

import gc
import json
import weakref
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

import kopf
import pytest
from kubernetes.client.exceptions import ApiException
from test_controller import APIs

import customer_cluster_operator.reconciliation_lock as lock_module
from customer_cluster_operator import controller
from customer_cluster_operator.models import ProvisioningInput

TIMEOUT = 5


class ObservedLock:
    """Expose contention while retaining native threading.Lock acquisition semantics."""

    def __init__(self, on_contention: Callable[[], None]):
        self._native = Lock()
        self._on_contention = on_contention

    def __enter__(self):
        if not self._native.acquire(blocking=False):
            self._on_contention()
            self._native.acquire()
        return self

    def __exit__(self, *_):
        self._native.release()

    def locked(self):
        return self._native.locked()


@pytest.fixture
def spawn():
    threads = []

    def start(name, callback):
        result = Future()

        def run():
            try:
                result.set_result(callback())
            except BaseException as exc:
                result.set_exception(exc)

        thread = Thread(name=name, target=run, daemon=True)
        threads.append(thread)
        thread.start()
        return result

    yield start
    for thread in threads:
        thread.join(timeout=TIMEOUT)
    assert not [thread.name for thread in threads if thread.is_alive()]


def configure_controller(monkeypatch, apis):
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/worker:1")
    monkeypatch.setattr(controller, "get_apis", lambda: apis)
    monkeypatch.setattr(controller, "_verification_bucket", lambda settings: 7)


def arguments(spec, body, patch):
    return {
        "spec": deepcopy(spec), "body": deepcopy(body), "status": {}, "patch": patch,
        "namespace": body["metadata"]["namespace"], "name": body["metadata"]["name"],
    }


def test_timer_change_race_serializes_profile_read_and_empty_job_decision(
    monkeypatch, spawn, spec, profile, body,
):
    body = deepcopy(body)
    body["metadata"]["uid"] = "timer-change-race-uid"
    apis = APIs(profile)
    custom, core, batch = apis
    configure_controller(monkeypatch, apis)
    state_guard = Lock()
    current_profile = deepcopy(custom.get_cluster_custom_object.return_value)
    jobs = []
    config_maps = []
    trace = []
    before_create = Event()
    allow_create = Event()
    change_attempted = Event()
    change_contended = Event()
    real_reconciliation_lock = controller.reconciliation_lock

    @contextmanager
    def observed_reconciliation_lock(namespace, name, uid):
        assert (namespace, name, uid) == (
            body["metadata"]["namespace"], body["metadata"]["name"], body["metadata"]["uid"],
        )
        if current_thread().name == "change":
            change_attempted.set()
        with real_reconciliation_lock(namespace, name, uid):
            yield

    def on_contention():
        assert current_thread().name == "change"
        change_contended.set()

    monkeypatch.setattr(controller, "reconciliation_lock", observed_reconciliation_lock)
    monkeypatch.setattr(lock_module, "Lock", lambda: ObservedLock(on_contention))

    def read_profile(*_):
        with state_guard:
            value = deepcopy(current_profile)
            trace.append(("profile", current_thread().name, value["metadata"]["generation"]))
            return value

    def list_jobs(*_, **__):
        with state_guard:
            trace.append(("jobs", current_thread().name, len(jobs)))
            return SimpleNamespace(items=list(jobs))

    def list_config_maps(*_, **__):
        with state_guard:
            return SimpleNamespace(items=list(config_maps))

    def create_config_map(namespace, config_map):
        if current_thread().name == "timer":
            before_create.set()
            assert allow_create.wait(TIMEOUT), "timer was never allowed to finish creation"
        with state_guard:
            config_maps.append(config_map)
            trace.append(("configmap", current_thread().name, config_map.metadata.name))
        return config_map

    def create_job(namespace, job):
        with state_guard:
            job.metadata.uid = "new-pending-job-uid"
            assert job.status is None
            jobs.append(job)
            trace.append(("created", current_thread().name, job.metadata.name))
        return job

    custom.get_cluster_custom_object.side_effect = read_profile
    batch.list_namespaced_job.side_effect = list_jobs
    core.list_namespaced_config_map.side_effect = list_config_maps
    core.create_namespaced_config_map.side_effect = create_config_map
    batch.create_namespaced_job.side_effect = create_job
    timer_patch, change_patch = kopf.Patch(), kopf.Patch()
    timer = spawn("timer", lambda: controller.on_timer(**arguments(spec, body, timer_patch)))
    try:
        assert before_create.wait(TIMEOUT), "timer did not reach its creation decision"
        assert trace == [("profile", "timer", 1), ("jobs", "timer", 0)]
        with state_guard:
            current_profile["metadata"]["generation"] = 2
            current_profile["spec"]["ansible"]["nodeInterface"] = "enp1s0"
        change = spawn("change", lambda: controller.on_change(
            **arguments(spec, body, change_patch),
        ))
        assert change_attempted.wait(TIMEOUT), "change handler bypassed the public lock wrapper"
        assert change_contended.wait(TIMEOUT), "change handler did not contend on the timer's lock"
        assert not change.done()
        custom.get_cluster_custom_object.assert_called_once()
        batch.list_namespaced_job.assert_called_once()
        batch.create_namespaced_job.assert_not_called()
        assert jobs == config_maps == []
    finally:
        allow_create.set()

    timer.result(timeout=TIMEOUT)
    change.result(timeout=TIMEOUT)
    assert timer_patch.status["conditions"][0]["reason"] == "ProvisioningJobCreated"
    assert change_patch.status["phase"] == "ProvisioningInfrastructure"
    assert change_patch.status["conditions"][0]["reason"] == "ProvisioningJobRunning"
    assert change_patch.status["conditions"][0]["status"] == "False"
    assert timer_patch.status["jobName"] == change_patch.status["jobName"] == jobs[0].metadata.name
    core.create_namespaced_config_map.assert_called_once()
    batch.create_namespaced_job.assert_called_once()
    assert custom.get_cluster_custom_object.call_count == batch.list_namespaced_job.call_count == 2
    assert trace == [
        ("profile", "timer", 1), ("jobs", "timer", 0),
        ("configmap", "timer", config_maps[0].metadata.name),
        ("created", "timer", jobs[0].metadata.name),
        ("profile", "change", 2), ("jobs", "change", 1),
    ]
    first_input = ProvisioningInput(json.loads(config_maps[0].data["input.json"]))
    assert first_input.data["profileRevision"] == {"uid": "profile-uid", "generation": 1}
    assert first_input.data["inventory"]["nodeInterface"] == "ens3"
    changed_data = deepcopy(first_input.data)
    changed_data["profileRevision"]["generation"] = 2
    changed_data["inventory"]["nodeInterface"] = "enp1s0"
    second_input = ProvisioningInput(changed_data)
    assert second_input.publication_hash != first_input.publication_hash
    assert second_input.input_hash == first_input.input_hash == change_patch.status["inputHash"]
    assert config_maps[0].immutable is True
    core.list_namespaced_pod.assert_not_called()
    core.delete_namespaced_config_map.assert_not_called()
    batch.delete_namespaced_job.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ["profile-api", "job-create-api", "invalid-spec", "invalid-profile", "invalid-generation"],
)
def test_controller_releases_lock_after_api_exception_or_invalid_input(
    monkeypatch, spawn, spec, profile, body, failure,
):
    apis = APIs(profile)
    configure_controller(monkeypatch, apis)
    invalid_spec, invalid_body = deepcopy(spec), deepcopy(body)
    if failure == "profile-api":
        apis[0].get_cluster_custom_object.side_effect = ApiException(status=503)
    elif failure == "job-create-api":
        apis[2].create_namespaced_job.side_effect = ApiException(status=503)
    elif failure == "invalid-spec":
        invalid_spec["suspend"] = "not-a-boolean"
    elif failure == "invalid-profile":
        apis[0].get_cluster_custom_object.return_value["metadata"]["generation"] = 0
    else:
        invalid_body["metadata"]["generation"] = "not-an-integer"
    failed = kopf.Patch()
    if failure.endswith("-api"):
        with pytest.raises(ApiException) as error:
            controller.on_timer(**arguments(invalid_spec, invalid_body, failed))
        assert error.value.status == 503
    elif failure == "invalid-generation":
        with pytest.raises(ValueError):
            controller.on_timer(**arguments(invalid_spec, invalid_body, failed))
    else:
        controller.on_timer(**arguments(invalid_spec, invalid_body, failed))
        assert failed.status["phase"] == "Failed"
        assert failed.status["conditions"][0]["reason"] == "InvalidConfiguration"

    apis[0].get_cluster_custom_object.side_effect = None
    apis[0].get_cluster_custom_object.return_value["metadata"]["generation"] = 1
    apis[2].create_namespaced_job.side_effect = None
    recovered = kopf.Patch()
    following = spawn("retry", lambda: controller.on_change(**arguments(spec, body, recovered)))
    following.result(timeout=TIMEOUT)
    assert recovered.status["phase"] == "ProvisioningInfrastructure"
    assert recovered.status["conditions"][0]["reason"] == "ProvisioningJobCreated"


@pytest.mark.parametrize("identity_field", ["namespace", "name", "uid"])
def test_independent_cluster_identities_progress_while_another_reconcile_is_held(
    monkeypatch, spawn, spec, body, identity_field,
):
    first_entered = Event()
    second_entered = Event()
    release_first = Event()
    second_body = deepcopy(body)
    second_body["metadata"][identity_field] = f"other-{identity_field}"

    def core(*, body, **_):
        if body["metadata"][identity_field] == second_body["metadata"][identity_field]:
            second_entered.set()
        else:
            first_entered.set()
            assert release_first.wait(TIMEOUT)

    monkeypatch.setattr(controller, "_reconcile_locked", core)
    first = spawn("first", lambda: controller.on_timer(**arguments(spec, body, kopf.Patch())))
    try:
        assert first_entered.wait(TIMEOUT)
        second = spawn("second", lambda: controller.on_change(
            **arguments(spec, second_body, kopf.Patch()),
        ))
        assert second_entered.wait(TIMEOUT), "an independent identity was blocked by the first"
        second.result(timeout=TIMEOUT)
        assert not first.done()
    finally:
        release_first.set()
    first.result(timeout=TIMEOUT)


def test_weak_registry_keeps_same_lock_alive_for_a_waiting_caller(monkeypatch, spawn):
    identity = ("lock-test", "waiting-cluster", "waiting-uid")
    contended = Event()
    resume_acquisition = Event()
    waiter_entered = Event()
    release_waiter = Event()

    def on_contention():
        assert current_thread().name == "waiter"
        contended.set()
        assert resume_acquisition.wait(TIMEOUT)

    monkeypatch.setattr(lock_module, "Lock", lambda: ObservedLock(on_contention))

    def waiting_caller():
        with lock_module.reconciliation_lock(*identity):
            waiter_entered.set()
            assert release_waiter.wait(TIMEOUT)

    try:
        with lock_module.reconciliation_lock(*identity):
            lock_ref = weakref.ref(lock_module._locks[identity])
            waiter = spawn("waiter", waiting_caller)
            assert contended.wait(TIMEOUT)
        gc.collect()
        assert lock_ref() is not None
        assert lock_module._locks.get(identity) is lock_ref()

        with lock_module.reconciliation_lock(*identity):
            assert lock_module._locks[identity] is lock_ref()
        resume_acquisition.set()
        assert waiter_entered.wait(TIMEOUT)
        assert lock_ref().locked()
        assert lock_module._locks.get(identity) is lock_ref()
    finally:
        resume_acquisition.set()
        release_waiter.set()
    waiter.result(timeout=TIMEOUT)
    gc.collect()
    assert lock_ref() is None
    assert identity not in lock_module._locks


def test_weak_registry_does_not_retain_unused_cluster_identities():
    gc.collect()
    with lock_module._guard:
        original_identities = set(lock_module._locks)
    references = []
    for index in range(128):
        identity = ("lock-test", f"completed-{index}", f"completed-uid-{index}")
        with lock_module.reconciliation_lock(*identity):
            references.append(weakref.ref(lock_module._locks[identity]))
            assert references[-1]() is not None
    gc.collect()
    assert all(reference() is None for reference in references)
    with lock_module._guard:
        assert set(lock_module._locks) == original_identities
