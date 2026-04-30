"""Tests for SSE-KMS transit key management."""

from unittest.mock import patch

import pytest

from resources.kms import ensure_transit_key, kms_key_name


class TestKmsKeyName:
    def test_format(self):
        assert kms_key_name("abc123") == "kms-abc123"


class TestEnsureTransitKeyNoConfig:
    def test_returns_false_when_bao_addr_unset(self, monkeypatch):
        monkeypatch.delenv("BAO_ADDR", raising=False)
        assert ensure_transit_key("any-project-id") is False

    def test_returns_false_when_token_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BAO_ADDR", "https://bao.example/")
        monkeypatch.setenv("BAO_TOKEN_FILE", str(tmp_path / "missing"))
        assert ensure_transit_key("any-project-id") is False

    def test_returns_false_when_token_file_empty(self, monkeypatch, tmp_path):
        token = tmp_path / "token"
        token.write_text("")
        monkeypatch.setenv("BAO_ADDR", "https://bao.example/")
        monkeypatch.setenv("BAO_TOKEN_FILE", str(token))
        assert ensure_transit_key("any-project-id") is False


class TestEnsureTransitKeyConfigured:
    @pytest.fixture
    def configured(self, monkeypatch, tmp_path):
        token = tmp_path / "token"
        token.write_text("s.testtoken")
        monkeypatch.setenv("BAO_ADDR", "https://bao.example")
        monkeypatch.setenv("BAO_TOKEN_FILE", str(token))

    def test_existing_key_returns_true(self, configured):
        with patch("resources.kms._bao_request") as req:
            req.return_value = (200, b'{"data":{"name":"kms-abc"}}')
            assert ensure_transit_key("abc") is True
            req.assert_called_once_with("GET", "/transit/keys/kms-abc")

    def test_creates_missing_key(self, configured):
        with patch("resources.kms._bao_request") as req:
            req.side_effect = [(404, b'{}'), (204, b"")]
            assert ensure_transit_key("abc") is True
            assert req.call_count == 2
            method, path, body = req.call_args_list[1][0]
            assert (method, path) == ("POST", "/transit/keys/kms-abc")
            assert body == {"type": "aes256-gcm96"}

    def test_unexpected_get_status_raises(self, configured):
        with patch("resources.kms._bao_request") as req:
            req.return_value = (500, b"oops")
            with pytest.raises(RuntimeError, match="500"):
                ensure_transit_key("abc")

    def test_unexpected_create_status_raises(self, configured):
        with patch("resources.kms._bao_request") as req:
            req.side_effect = [(404, b"{}"), (403, b"forbidden")]
            with pytest.raises(RuntimeError, match="403"):
                ensure_transit_key("abc")
