"""Tests for periodic OpenstackProject reconciliation."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# `src/handlers.py` is shadowed by the `src/handlers/` package on normal
# import; load it the same way Kopf does in production.
_spec = importlib.util.spec_from_file_location(
    "periodic_project_handlers", Path(__file__).parent.parent / "src" / "handlers.py"
)
handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handlers)


@pytest.fixture
def reconcile_env(monkeypatch):
    client = MagicMock()
    apply_roles = MagicMock()
    get_federation_config = MagicMock(
        return_value={
            "idp_name": "idp",
            "idp_remote_id": "remote-id",
            "sso_domain": "sso-users",
        }
    )
    federation_manager = MagicMock()
    registry = MagicMock()
    monkeypatch.setattr(handlers, "get_openstack_client", lambda: client)
    monkeypatch.setattr(
        handlers,
        "get_project_info",
        lambda *_: {"project_id": "pid-123", "group_id": "gid-456"},
    )
    monkeypatch.setattr(handlers, "ensure_transit_key", lambda *_: True)
    monkeypatch.setattr(handlers, "_resolve_group_id", lambda *_: "gid-456")
    monkeypatch.setattr(handlers, "apply_role_bindings", apply_roles)
    monkeypatch.setattr(handlers, "get_federation_config", get_federation_config)
    monkeypatch.setattr(handlers, "FederationManager", federation_manager)
    monkeypatch.setattr(handlers, "get_registry", lambda: registry)
    return client, apply_roles, get_federation_config, federation_manager, registry


def _reconcile(spec):
    patch = SimpleNamespace(status={})
    handlers.reconcile_project(
        spec=spec,
        status={"phase": "Ready", "projectId": "pid-123", "groupId": "gid-456"},
        patch=patch,
        namespace="customer-projects",
        name="example-project",
    )
    return patch


def test_periodic_reconcile_uses_direct_user_roles_for_managed_project(reconcile_env):
    client, apply_roles, get_federation_config, federation_manager, registry = reconcile_env
    role_bindings = [{"role": "reader", "users": ["admin@example.org"]}]

    _reconcile(
        {
            "name": "example",
            "domain": "sso-users",
            "managed": True,
            "federationRef": {
                "configMapName": "federation-config",
                "configMapNamespace": "openstack-operator",
            },
            "roleBindings": role_bindings,
        }
    )

    apply_roles.assert_called_once_with(
        client,
        "pid-123",
        "gid-456",
        role_bindings,
        "sso-users",
        managed=True,
    )
    get_federation_config.assert_called_once()
    federation_manager.return_value.remove_project_mapping.assert_called_once_with("example")
    registry.unregister.assert_called_once_with("federation_mappings", "example")


def test_periodic_reconcile_uses_group_roles_for_unmanaged_project(reconcile_env):
    client, apply_roles, _, _, _ = reconcile_env
    role_bindings = [{"role": "member", "users": ["member@example.org"]}]

    _reconcile(
        {
            "name": "example",
            "domain": "sso-users",
            "roleBindings": role_bindings,
        }
    )

    apply_roles.assert_called_once_with(
        client,
        "pid-123",
        "gid-456",
        role_bindings,
        "sso-users",
        managed=False,
    )
