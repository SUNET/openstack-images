"""Tests for utility functions."""

import datetime
from utils import (
    find_duplicate_project_crs,
    find_project_owner_cr,
    is_valid_uuid,
    make_group_name,
    now_iso,
    sanitize_name,
    set_condition,
)


def _make_cr(
    namespace: str,
    name: str,
    project_name: str,
    domain: str = "sso-users",
    created: str = "2026-01-01T00:00:00Z",
    project_id: str | None = None,
    deleting: bool = False,
) -> dict:
    cr: dict = {
        "metadata": {
            "namespace": namespace,
            "name": name,
            "creationTimestamp": created,
        },
        "spec": {"name": project_name, "domain": domain},
        "status": {},
    }
    if project_id:
        cr["status"]["projectId"] = project_id
    if deleting:
        cr["metadata"]["deletionTimestamp"] = "2026-01-02T00:00:00Z"
    return cr


class TestIsValidUuid:
    """Tests for is_valid_uuid function."""

    def test_valid_uuid_v4(self):
        assert is_valid_uuid("7581eb5e-69a1-4d73-9608-015b7fbfe1fb") is True

    def test_valid_uuid_without_hyphens(self):
        assert is_valid_uuid("7581eb5e69a14d739608015b7fbfe1fb") is True

    def test_valid_uuid_uppercase(self):
        assert is_valid_uuid("7581EB5E-69A1-4D73-9608-015B7FBFE1FB") is True

    def test_invalid_group_name(self):
        assert is_valid_uuid("platform-test-sunet-se-users") is False

    def test_invalid_empty_string(self):
        assert is_valid_uuid("") is False

    def test_invalid_none(self):
        assert is_valid_uuid(None) is False

    def test_invalid_short_string(self):
        assert is_valid_uuid("abc123") is False


class TestSanitizeName:
    """Tests for sanitize_name function."""

    def test_lowercase(self):
        assert sanitize_name("MyProject") == "myproject"

    def test_dots_to_hyphens(self):
        assert sanitize_name("my.project.com") == "my-project-com"

    def test_underscores_to_hyphens(self):
        assert sanitize_name("my_project_name") == "my-project-name"

    def test_removes_special_chars(self):
        assert sanitize_name("my@project!name") == "myprojectname"

    def test_collapses_multiple_hyphens(self):
        assert sanitize_name("my--project") == "my-project"
        assert sanitize_name("my...project") == "my-project"

    def test_strips_leading_trailing_hyphens(self):
        assert sanitize_name("-project-") == "project"
        assert sanitize_name("...project...") == "project"

    def test_complex_example(self):
        assert sanitize_name("My_Project.Example.COM") == "my-project-example-com"


class TestMakeGroupName:
    """Tests for make_group_name function."""

    def test_appends_users_suffix(self):
        assert make_group_name("my-project") == "my-project-users"

    def test_sanitizes_input(self):
        assert make_group_name("My_Project.COM") == "my-project-com-users"


class TestNowIso:
    """Tests for now_iso function."""

    def test_returns_iso_format(self):
        result = now_iso()
        # Should be parseable as ISO format
        parsed = datetime.datetime.fromisoformat(result)
        assert parsed is not None

    def test_returns_utc(self):
        result = now_iso()
        parsed = datetime.datetime.fromisoformat(result)
        assert parsed.tzinfo is not None


class TestSetCondition:
    """Tests for set_condition function."""

    def test_adds_new_condition(self):
        status: dict = {}
        set_condition(status, "Ready", "True", "Completed", "All done")

        assert len(status["conditions"]) == 1
        assert status["conditions"][0]["type"] == "Ready"
        assert status["conditions"][0]["status"] == "True"
        assert status["conditions"][0]["reason"] == "Completed"
        assert status["conditions"][0]["message"] == "All done"
        assert "lastTransitionTime" in status["conditions"][0]

    def test_updates_existing_condition(self):
        status: dict = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "Pending",
                    "message": "",
                    "lastTransitionTime": "2024-01-01T00:00:00+00:00",
                }
            ]
        }
        set_condition(status, "Ready", "True", "Completed", "Done")

        assert len(status["conditions"]) == 1
        assert status["conditions"][0]["status"] == "True"
        assert status["conditions"][0]["reason"] == "Completed"
        # Transition time should be updated since status changed
        assert status["conditions"][0]["lastTransitionTime"] != "2024-01-01T00:00:00+00:00"

    def test_preserves_transition_time_if_status_unchanged(self):
        original_time = "2024-01-01T00:00:00+00:00"
        status: dict = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "Completed",
                    "message": "Done",
                    "lastTransitionTime": original_time,
                }
            ]
        }
        set_condition(status, "Ready", "True", "StillComplete", "Still done")

        assert status["conditions"][0]["lastTransitionTime"] == original_time
        assert status["conditions"][0]["reason"] == "StillComplete"

    def test_multiple_conditions(self):
        status: dict = {}
        set_condition(status, "Ready", "True", "", "")
        set_condition(status, "NetworkReady", "False", "Pending", "")

        assert len(status["conditions"]) == 2
        types = {c["type"] for c in status["conditions"]}
        assert types == {"Ready", "NetworkReady"}


class TestFindDuplicateProjectCrs:
    """Tests for find_duplicate_project_crs function."""

    def test_no_duplicates(self):
        crs = [_make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se")]
        assert (
            find_duplicate_project_crs(
                crs, "customer-projects", "drive-sunet-se", "drive.sunet.se", "sso-users"
            )
            == []
        )

    def test_excludes_self(self):
        crs = [
            _make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se"),
            _make_cr("openstack-operator", "drive", "drive.sunet.se"),
        ]
        dups = find_duplicate_project_crs(
            crs, "customer-projects", "drive-sunet-se", "drive.sunet.se", "sso-users"
        )
        assert len(dups) == 1
        assert dups[0]["metadata"]["name"] == "drive"

    def test_same_cr_name_other_namespace_is_duplicate(self):
        crs = [
            _make_cr("customer-projects", "drive", "drive.sunet.se"),
            _make_cr("openstack-operator", "drive", "drive.sunet.se"),
        ]
        dups = find_duplicate_project_crs(
            crs, "customer-projects", "drive", "drive.sunet.se", "sso-users"
        )
        assert len(dups) == 1
        assert dups[0]["metadata"]["namespace"] == "openstack-operator"

    def test_different_domain_not_duplicate(self):
        crs = [
            _make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se"),
            _make_cr("openstack-operator", "drive", "drive.sunet.se", domain="other"),
        ]
        assert (
            find_duplicate_project_crs(
                crs, "customer-projects", "drive-sunet-se", "drive.sunet.se", "sso-users"
            )
            == []
        )

    def test_different_project_name_not_duplicate(self):
        crs = [
            _make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se"),
            _make_cr("customer-projects", "jupyter-sunet-se", "jupyter.sunet.se"),
        ]
        assert (
            find_duplicate_project_crs(
                crs, "customer-projects", "drive-sunet-se", "drive.sunet.se", "sso-users"
            )
            == []
        )


class TestFindProjectOwnerCr:
    """Tests for find_project_owner_cr function."""

    def test_single_cr_may_proceed(self):
        crs = [_make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se")]
        assert (
            find_project_owner_cr(
                crs,
                "customer-projects",
                "drive-sunet-se",
                "2026-01-01T00:00:00Z",
                "drive.sunet.se",
                "sso-users",
            )
            is None
        )

    def test_provisioned_duplicate_owns(self):
        crs = [
            _make_cr("customer-projects", "drive-sunet-se", "drive.sunet.se",
                     created="2026-01-01T00:00:00Z"),
            _make_cr("openstack-operator", "drive", "drive.sunet.se",
                     created="2026-06-01T00:00:00Z", project_id="abc123"),
        ]
        # Even though the other CR is newer, it already provisioned the project
        owner = find_project_owner_cr(
            crs,
            "customer-projects",
            "drive-sunet-se",
            "2026-01-01T00:00:00Z",
            "drive.sunet.se",
            "sso-users",
        )
        assert owner == "openstack-operator/drive"

    def test_older_unprovisioned_duplicate_owns(self):
        crs = [
            _make_cr("openstack-operator", "drive", "drive.sunet.se",
                     created="2026-01-01T00:00:00Z"),
        ]
        owner = find_project_owner_cr(
            crs,
            "customer-projects",
            "drive-sunet-se",
            "2026-06-01T00:00:00Z",
            "drive.sunet.se",
            "sso-users",
        )
        assert owner == "openstack-operator/drive"

    def test_newer_unprovisioned_duplicate_does_not_own(self):
        crs = [
            _make_cr("openstack-operator", "drive", "drive.sunet.se",
                     created="2026-06-01T00:00:00Z"),
        ]
        assert (
            find_project_owner_cr(
                crs,
                "customer-projects",
                "drive-sunet-se",
                "2026-01-01T00:00:00Z",
                "drive.sunet.se",
                "sso-users",
            )
            is None
        )

    def test_terminating_duplicate_ignored(self):
        crs = [
            _make_cr("openstack-operator", "drive", "drive.sunet.se",
                     created="2026-01-01T00:00:00Z", project_id="abc123",
                     deleting=True),
        ]
        assert (
            find_project_owner_cr(
                crs,
                "customer-projects",
                "drive-sunet-se",
                "2026-06-01T00:00:00Z",
                "drive.sunet.se",
                "sso-users",
            )
            is None
        )

    def test_timestamp_tie_breaks_on_namespace_name(self):
        ts = "2026-01-01T00:00:00Z"
        crs = [
            _make_cr("a-namespace", "drive", "drive.sunet.se", created=ts),
        ]
        owner = find_project_owner_cr(
            crs, "customer-projects", "drive-sunet-se", ts,
            "drive.sunet.se", "sso-users",
        )
        assert owner == "a-namespace/drive"
