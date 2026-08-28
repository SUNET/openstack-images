"""Tests for the delete handler's duplicate-CR / fail-closed guard.

Regression for the 2026-07-02 drive.sunet.se incident: pruning a static CR
deleted the live Keystone project even though a portal CR still referenced
it. The delete handler must skip OpenStack deletion when a duplicate CR
exists, and must fail CLOSED (retry, not delete) when the CR list cannot
be fetched.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import kopf
import pytest

# `src/handlers.py` is shadowed by the `src/handlers/` package on normal
# import; the operator runs it by path (`kopf run src/handlers.py`), so
# load it the same way here.
_spec = importlib.util.spec_from_file_location(
    "project_handlers", Path(__file__).parent.parent / "src" / "handlers.py"
)
handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handlers)


@pytest.fixture
def delete_env(monkeypatch):
    """Mock everything around delete_project_handler; return the mocks."""
    client = MagicMock()
    deleted = MagicMock()
    monkeypatch.setattr(handlers, "get_openstack_client", lambda: client)
    monkeypatch.setattr(handlers, "delete_project", deleted)
    monkeypatch.setattr(handlers.kopf, "warn", lambda *a, **k: None)
    return {"client": client, "delete_project": deleted}


SPEC = {"name": "drive.sunet.se", "domain": "sso-users"}
STATUS = {"projectId": "pid-123", "groupId": "gid-456"}


def _run(namespace="openstack-operator", name="drive"):
    handlers.delete_project_handler(
        spec=SPEC,
        status=STATUS,
        namespace=namespace,
        name=name,
        body={},
    )


def test_cr_list_failure_refuses_to_delete(delete_env, monkeypatch):
    """Listing CRs failed -> unknown ownership -> TemporaryError, no delete."""
    monkeypatch.setattr(handlers, "_list_project_crs", lambda: None)
    with pytest.raises(kopf.TemporaryError):
        _run()
    delete_env["delete_project"].assert_not_called()


def test_duplicate_cr_skips_openstack_deletion(delete_env, monkeypatch):
    """Another CR targets the same project -> remove only this CR."""
    duplicate = {
        "metadata": {"namespace": "customer-projects", "name": "drive-sunet-se"},
        "spec": dict(SPEC),
    }
    monkeypatch.setattr(handlers, "_list_project_crs", lambda: [duplicate])
    _run()
    delete_env["delete_project"].assert_not_called()


def test_sole_cr_deletes_project(delete_env, monkeypatch):
    """No other CR references the project -> deletion proceeds."""
    me = {
        "metadata": {"namespace": "openstack-operator", "name": "drive"},
        "spec": dict(SPEC),
    }
    monkeypatch.setattr(handlers, "_list_project_crs", lambda: [me])
    _run()
    delete_env["delete_project"].assert_called_once_with(
        delete_env["client"], "pid-123", "gid-456", "sso-users"
    )


def test_contract_tag_annotation_runs_tag_only_update(monkeypatch):
    """The repair trigger must not reconcile users or federation as a side effect."""
    client = MagicMock()
    apply_roles = MagicMock()
    get_federation = MagicMock()
    monkeypatch.setattr(handlers, "get_openstack_client", lambda: client)
    monkeypatch.setattr(handlers, "_resolve_group_id", lambda *args: "gid-456")
    monkeypatch.setattr(handlers, "apply_role_bindings", apply_roles)
    monkeypatch.setattr(handlers, "get_federation_config", get_federation)

    patch = SimpleNamespace(status={})
    handlers.update_project(
        spec={
            "name": "platform.sunet.se",
            "domain": "sso-users",
            "contractNumber": "Platform-2615-40257",
            "roleBindings": [{"role": "member", "users": ["user@sunet.se"]}],
            "federationRef": {"configMapName": "federation-config"},
        },
        status=STATUS,
        patch=patch,
        namespace="customer-projects",
        name="platform-sunet-se",
        meta={"generation": 1},
        diff=(
            (
                "change",
                (
                    "metadata",
                    "annotations",
                    handlers.CONTRACT_TAG_RECONCILE_ANNOTATION,
                ),
                None,
                "openstack-operator-0.1.4-20260828-1",
            ),
        ),
        body={},
    )

    client.set_project_contract_tag.assert_called_once_with("pid-123", "Platform-2615-40257")
    apply_roles.assert_not_called()
    get_federation.assert_not_called()
    assert patch.status["phase"] == "Ready"
