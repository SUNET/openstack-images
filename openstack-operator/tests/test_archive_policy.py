"""Tests for Gnocchi archive policy client methods.

Regression guard for the keystoneauth raise-on-4xx default: the session
raises on any 4xx unless raise_exc=False is passed, which made the 404
handling in get/delete_archive_policy dead code and left the operator
unable to bootstrap a missing policy (billing outage, 2026-07-06).
"""

from unittest.mock import Mock

import pytest

from openstack_client import OpenStackClient


class FakeKeystoneauthNotFound(Exception):
    """Stands in for keystoneauth1.exceptions.http.NotFound."""


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FakeKeystoneauthNotFound(f"HTTP {self.status_code}")


class FakeSession:
    """Mimics keystoneauth1 Session: raises on 4xx unless raise_exc=False."""

    def __init__(self, responses: dict[tuple[str, str], FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def _request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url))
        resp = self._responses[(method, url)]
        if kwargs.get("raise_exc", True) and resp.status_code >= 400:
            raise FakeKeystoneauthNotFound(
                f"Not Found (HTTP {resp.status_code})"
            )
        return resp

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._request("DELETE", url, **kwargs)

    def get_endpoint(self, **kwargs):
        return "http://gnocchi.test:8041"


GNOCCHI = "http://gnocchi.test:8041"


def make_client(responses: dict[tuple[str, str], FakeResponse]) -> tuple[
    OpenStackClient, FakeSession
]:
    client = OpenStackClient()
    session = FakeSession(responses)
    conn = Mock()
    conn.session = session
    client._conn = conn
    return client, session


def test_get_archive_policy_returns_none_on_404():
    url = f"{GNOCCHI}/v1/archive_policy/ceilometer-billing"
    client, _ = make_client({("GET", url): FakeResponse(404)})
    assert client.get_archive_policy("ceilometer-billing") is None


def test_get_archive_policy_returns_policy_when_present():
    url = f"{GNOCCHI}/v1/archive_policy/ceilometer-billing"
    body = {"name": "ceilometer-billing", "definition": []}
    client, _ = make_client({("GET", url): FakeResponse(200, body)})
    assert client.get_archive_policy("ceilometer-billing") == body


def test_get_archive_policy_raises_on_other_errors():
    url = f"{GNOCCHI}/v1/archive_policy/ceilometer-billing"
    client, _ = make_client({("GET", url): FakeResponse(500)})
    with pytest.raises(FakeKeystoneauthNotFound):
        client.get_archive_policy("ceilometer-billing")


def test_delete_archive_policy_404_is_noop():
    url = f"{GNOCCHI}/v1/archive_policy/ceilometer-billing"
    client, _ = make_client({("DELETE", url): FakeResponse(404)})
    client.delete_archive_policy("ceilometer-billing")


def test_ensure_archive_policy_creates_when_absent():
    """The absent-policy path must reach the create POST (the 2026-07-06
    outage: the GET raised before create could run)."""
    from resources.archive_policy import ensure_archive_policy

    get_url = f"{GNOCCHI}/v1/archive_policy/ceilometer-billing"
    post_url = f"{GNOCCHI}/v1/archive_policy"
    client, session = make_client(
        {
            ("GET", get_url): FakeResponse(404),
            ("POST", post_url): FakeResponse(
                201, {"name": "ceilometer-billing"}
            ),
        }
    )

    spec = {
        "name": "ceilometer-billing",
        "definition": [{"granularity": "5m", "timespan": "30d"}],
        "aggregationMethods": ["mean", "rate:mean"],
    }
    assert ensure_archive_policy(client, spec) == "ceilometer-billing"
    assert ("POST", post_url) in session.calls
