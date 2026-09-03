"""Tests for managed-project direct role bindings."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from resources.role_binding import apply_role_bindings


def test_managed_bindings_add_same_role_users_from_multiple_domains() -> None:
    client = MagicMock()
    client.get_role.return_value = SimpleNamespace(id="member-role")
    users = {
        ("portal-admin@example.org", "sso-users"): SimpleNamespace(id="admin-id"),
        ("openstack-operator", "default"): SimpleNamespace(id="operator-id"),
    }
    client.get_user.side_effect = lambda name, domain: users.get((name, domain))

    apply_role_bindings(
        client,
        "project-id",
        "group-id",
        [
            {
                "role": "member",
                "users": ["portal-admin@example.org"],
                "userDomain": "sso-users",
            },
            {
                "role": "member",
                "users": ["openstack-operator"],
                "userDomain": "default",
            },
        ],
        "sso-users",
        managed=True,
    )

    assert client.assign_role_to_user.call_args_list == [
        (("member-role", "admin-id", "project-id"),),
        (("member-role", "operator-id", "project-id"),),
    ]
    client.revoke_role_from_user.assert_not_called()
    client.list_user_role_assignments_on_project.assert_not_called()
