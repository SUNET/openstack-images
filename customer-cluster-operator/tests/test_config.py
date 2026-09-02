import pytest

from customer_cluster_operator.config import Settings
from customer_cluster_operator.errors import ValidationError


def test_verification_interval_defaults_to_fifteen_minutes(monkeypatch):
    monkeypatch.setenv("WORKER_IMAGE", "worker:1")
    monkeypatch.delenv("VERIFICATION_INTERVAL_SECONDS", raising=False)
    assert Settings.from_env().verification_interval == 900


def test_verification_interval_has_safe_minimum(monkeypatch):
    monkeypatch.setenv("WORKER_IMAGE", "worker:1")
    monkeypatch.setenv("VERIFICATION_INTERVAL_SECONDS", "30")
    with pytest.raises(ValidationError, match="at least 60"):
        Settings.from_env()
