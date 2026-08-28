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
