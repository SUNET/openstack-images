"""Tests for utility functions."""

import datetime
from utils import is_valid_uuid, sanitize_name, make_group_name, now_iso, set_condition


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
