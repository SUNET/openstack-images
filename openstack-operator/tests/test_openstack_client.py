"""Tests for OpenStack client service endpoint handling."""

from unittest.mock import MagicMock

import pytest

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

        assert client._gnocchi_url() == (
            "http://gnocchi-api.openstack.svc.cluster.local:8041"
        )
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
