"""Tests for OpenStack client service endpoint handling."""

from unittest.mock import MagicMock, call

import pytest

import openstack_client
from models import OpenStackAPIError
from openstack_client import OpenStackClient


class TestGnocchiEndpoint:
    """Tests for region- and interface-aware Gnocchi endpoint selection."""

    def test_uses_cloud_config_endpoint(self):
        connection = MagicMock()
        connection.config.get_session_endpoint.return_value = (
            "http://gnocchi-api.openstack.svc.cluster.local:8041/"
        )
        client = OpenStackClient()
        client._conn = connection

        assert client._gnocchi_url() == ("http://gnocchi-api.openstack.svc.cluster.local:8041")
        connection.config.get_session_endpoint.assert_called_once_with("metric")
        connection.session.get_endpoint.assert_not_called()

    def test_caches_configured_endpoint(self):
        connection = MagicMock()
        connection.config.get_session_endpoint.return_value = "https://gnocchi.internal/"
        client = OpenStackClient()
        client._conn = connection

        assert client._gnocchi_url() == "https://gnocchi.internal"
        assert client._gnocchi_url() == "https://gnocchi.internal"
        connection.config.get_session_endpoint.assert_called_once_with("metric")

    def test_rejects_missing_configured_endpoint(self):
        connection = MagicMock()
        connection.config.get_session_endpoint.return_value = None
        client = OpenStackClient()
        client._conn = connection

        with pytest.raises(OpenStackAPIError, match="No Gnocchi endpoint"):
            client._gnocchi_url()


class TestGnocchiArchivePolicyRequests:
    """Tests for keystoneauth response handling in archive policy requests."""

    @staticmethod
    def _client() -> tuple[OpenStackClient, MagicMock]:
        connection = MagicMock()
        connection.config.get_session_endpoint.return_value = "https://gnocchi.internal"
        client = OpenStackClient()
        client._conn = connection
        return client, connection

    def test_missing_archive_policy_returns_none(self):
        client, connection = self._client()
        response = MagicMock(status_code=404)
        connection.session.get.return_value = response

        assert client.get_archive_policy("missing") is None
        connection.session.get.assert_called_once_with(
            "https://gnocchi.internal/v1/archive_policy/missing",
            raise_exc=False,
        )

    def test_existing_archive_policy_is_returned(self):
        client, connection = self._client()
        policy = {"name": "ceilometer-billing"}
        response = MagicMock(status_code=200)
        response.json.return_value = policy
        connection.session.get.return_value = response

        assert client.get_archive_policy("ceilometer-billing") == policy

    def test_create_archive_policy_disables_keystoneauth_auto_raise(self):
        client, connection = self._client()
        policy = {"name": "ceilometer-billing"}
        response = MagicMock(status_code=201)
        response.json.return_value = policy
        connection.session.post.return_value = response

        result = client.create_archive_policy(
            name="ceilometer-billing",
            definition=[{"granularity": "5m", "timespan": "30d"}],
            aggregation_methods=["mean", "rate:mean"],
        )

        assert result == policy
        connection.session.post.assert_called_once_with(
            "https://gnocchi.internal/v1/archive_policy",
            json={
                "name": "ceilometer-billing",
                "definition": [{"granularity": "5m", "timespan": "30d"}],
                "aggregation_methods": ["mean", "rate:mean"],
                "back_window": 0,
            },
            raise_exc=False,
        )

    def test_update_archive_policy_disables_keystoneauth_auto_raise(self):
        client, connection = self._client()
        policy = {"name": "ceilometer-billing"}
        response = MagicMock(status_code=200)
        response.json.return_value = policy
        connection.session.patch.return_value = response

        result = client.update_archive_policy(
            name="ceilometer-billing",
            definition=[{"granularity": "1h", "timespan": "365d"}],
        )

        assert result == policy
        connection.session.patch.assert_called_once_with(
            "https://gnocchi.internal/v1/archive_policy/ceilometer-billing",
            json={"definition": [{"granularity": "1h", "timespan": "365d"}]},
            raise_exc=False,
        )

    def test_delete_missing_archive_policy_is_idempotent(self):
        client, connection = self._client()
        response = MagicMock(status_code=404)
        connection.session.delete.return_value = response

        client.delete_archive_policy("missing")

        connection.session.delete.assert_called_once_with(
            "https://gnocchi.internal/v1/archive_policy/missing",
            raise_exc=False,
        )


class TestProjectContractTags:
    """Tests for Git-authoritative Keystone contract-tag reconciliation."""

    @staticmethod
    def _client(tags: list[str]) -> tuple[OpenStackClient, MagicMock, MagicMock, set[str]]:
        connection = MagicMock()
        project = MagicMock()
        tag_state = set(tags)

        def fetch_tags(_session):
            project.tags = sorted(tag_state)
            return project

        def add_tag(_session, tag):
            tag_state.add(tag)
            return project

        def remove_tag(_session, tag):
            tag_state.discard(tag)
            return project

        project.fetch_tags.side_effect = fetch_tags
        project.add_tag.side_effect = add_tag
        project.remove_tag.side_effect = remove_tag
        connection.identity.get_project.return_value = project
        client = OpenStackClient()
        client._conn = connection
        return client, connection, project, tag_state

    def test_replaces_stale_contract_tags_and_preserves_other_tags(self):
        client, connection, project, tag_state = self._client(
            [
                "managed-by-openstack-operator",
                "customer-visible",
                "contractual:preserved",
                "contract:OLD",
                "contract:STALE",
            ]
        )

        client.set_project_contract_tag("project-1", "NEW")

        project.add_tag.assert_called_once_with(connection.identity, "contract:NEW")
        project.remove_tag.assert_has_calls(
            [
                call(connection.identity, "contract:OLD"),
                call(connection.identity, "contract:STALE"),
            ]
        )
        assert tag_state == {
            "contract:NEW",
            "contractual:preserved",
            "customer-visible",
            "managed-by-openstack-operator",
        }
        connection.identity.update_project.assert_not_called()

    def test_is_idempotent_when_contract_tag_is_already_exact(self):
        client, connection, project, _ = self._client(
            ["managed-by-openstack-operator", "contract:CURRENT"]
        )

        client.set_project_contract_tag("project-1", "CURRENT")

        project.add_tag.assert_not_called()
        project.remove_tag.assert_not_called()
        connection.identity.update_project.assert_not_called()

    def test_removes_contract_tags_when_git_has_no_contract(self):
        client, connection, project, tag_state = self._client(
            ["managed-by-openstack-operator", "contract:OLD"]
        )

        client.set_project_contract_tag("project-1", None)

        project.remove_tag.assert_called_once_with(connection.identity, "contract:OLD")
        assert tag_state == {"managed-by-openstack-operator"}
        connection.identity.update_project.assert_not_called()

    def test_preserves_unrelated_tag_added_during_reconciliation(self):
        client, connection, project, tag_state = self._client(
            ["managed-by-openstack-operator", "contract:OLD"]
        )
        original_add = project.add_tag.side_effect

        def add_with_concurrent_tag(session, tag):
            tag_state.add("concurrent-non-contract-tag")
            return original_add(session, tag)

        project.add_tag.side_effect = add_with_concurrent_tag

        client.set_project_contract_tag("project-1", "NEW")

        assert "concurrent-non-contract-tag" in tag_state
        connection.identity.update_project.assert_not_called()

    def test_retries_when_concurrent_contract_tag_breaks_verification(self, monkeypatch):
        client, connection, project, tag_state = self._client(
            ["managed-by-openstack-operator", "contract:OLD"]
        )
        original_fetch = project.fetch_tags.side_effect
        fetch_count = 0

        def fetch_with_contract_race(session):
            nonlocal fetch_count
            fetch_count += 1
            result = original_fetch(session)
            if fetch_count == 2:
                tag_state.add("contract:RACE")
                project.tags = sorted(tag_state)
            return result

        project.fetch_tags.side_effect = fetch_with_contract_race
        monkeypatch.setattr(openstack_client.time, "sleep", lambda _: None)

        client.set_project_contract_tag("project-1", "NEW")

        assert fetch_count == 4
        assert {tag for tag in tag_state if tag.startswith("contract:")} == {"contract:NEW"}
        project.remove_tag.assert_any_call(connection.identity, "contract:RACE")
