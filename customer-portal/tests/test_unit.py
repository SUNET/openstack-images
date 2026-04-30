"""Unit tests for pure-logic helpers (no DB, no network)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from app.kubeconfig_service import (
    _build_csr,
    _build_kubeconfig,
    _cert_serial_and_expiry,
    issuance_status,
)
from app.models import KubeconfigIssuance
from app.schemas import (
    AddonRequestPayload,
    BackupRequestPayload,
    CreateClusterRequest,
    ResizeRequestPayload,
    _size_label,
)


# --- _size_label ---


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "Liten"),
        (2, "Mellan"),
        (3, "Stor"),
        (4, "XL"),
        (5, "XXL"),
        (6, "XXXL"),
        (7, "XXXXL"),
        (10, "XXXXXXXL"),
    ],
)
def test_size_label(n: int, expected: str) -> None:
    assert _size_label(n) == expected


def test_size_label_invalid() -> None:
    assert "Invalid" in _size_label(0)
    assert "Invalid" in _size_label(-1)


# --- CSR / kubeconfig builders ---


def test_build_csr_round_trips() -> None:
    csr_pem, key_pem = _build_csr(common_name="oidc:abc", organization="org-X")
    # Both parse back as valid PEM objects.
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    cn = csr.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    org = csr.subject.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)[0].value
    assert cn == "oidc:abc"
    assert org == "org-X"
    # Public-key match between CSR and the generated key.
    assert (
        csr.public_key().public_numbers() == key.public_key().public_numbers()
    )


def test_build_kubeconfig_shape() -> None:
    yml = _build_kubeconfig(
        cluster_name="acme",
        api_url="https://k8s.acme.test:6443",
        ca_bundle="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        cert_pem="-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----\n",
        key_pem="-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n",
        user_name="oidc:bob",
    )
    cfg = yaml.safe_load(yml)
    assert cfg["apiVersion"] == "v1"
    assert cfg["kind"] == "Config"
    assert cfg["current-context"] == "acme"
    assert cfg["clusters"][0]["name"] == "acme"
    assert cfg["clusters"][0]["cluster"]["server"] == "https://k8s.acme.test:6443"
    # CA / cert / key are b64-encoded into kubeconfig.
    ca_b64 = cfg["clusters"][0]["cluster"]["certificate-authority-data"]
    cert_b64 = cfg["users"][0]["user"]["client-certificate-data"]
    key_b64 = cfg["users"][0]["user"]["client-key-data"]
    assert "BEGIN CERTIFICATE" in base64.b64decode(ca_b64).decode()
    assert "BEGIN CERTIFICATE" in base64.b64decode(cert_b64).decode()
    assert "BEGIN PRIVATE KEY" in base64.b64decode(key_b64).decode()
    assert cfg["contexts"][0]["context"]["namespace"] == "argocd"
    assert cfg["users"][0]["name"] == "oidc:bob"


def test_cert_serial_and_expiry_parses_real_cert() -> None:
    # Build a minimal self-signed cert and feed it through.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test")])
    not_after = datetime.now(timezone.utc) + timedelta(days=365)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0xDEADBEEF)
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    serial, expires = _cert_serial_and_expiry(pem)
    assert serial == "deadbeef"
    # Stored expiry is naive; compare against naive view of not_after.
    assert abs(expires - not_after.replace(tzinfo=None)).total_seconds() < 2


# --- Issuance status ---


def _issuance(*, expires_in_days: int, revoked: bool = False) -> KubeconfigIssuance:
    iss = KubeconfigIssuance(
        cluster_id=1,
        user_sub="u",
        label="l",
        cert_serial="abc",
        rolebinding_name="rb",
        cert_group="g",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(days=expires_in_days),
    )
    if revoked:
        iss.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return iss


def test_issuance_status_active() -> None:
    assert issuance_status(_issuance(expires_in_days=30)) == "active"


def test_issuance_status_expired() -> None:
    assert issuance_status(_issuance(expires_in_days=-1)) == "expired"


def test_issuance_status_revoked_takes_precedence() -> None:
    # Revoked + expired → still 'revoked' (not expired).
    assert (
        issuance_status(_issuance(expires_in_days=-1, revoked=True)) == "revoked"
    )


# --- Schema payload validation ---


def test_addon_payload_ok() -> None:
    p = AddonRequestPayload(action="enable", addon_type="jupyterhub")
    assert p.action == "enable"


def test_addon_payload_action_must_be_enable_or_disable() -> None:
    with pytest.raises(ValidationError):
        AddonRequestPayload(action="install", addon_type="jupyterhub")


def test_resize_payload_requires_positive_target() -> None:
    with pytest.raises(ValidationError):
        ResizeRequestPayload(target_worker_groups=0)


def test_backup_payload_action_validated() -> None:
    BackupRequestPayload(action="enable")
    BackupRequestPayload(action="disable")
    with pytest.raises(ValidationError):
        BackupRequestPayload(action="toggle")


def test_create_cluster_request_slug_regex() -> None:
    base = dict(
        contract_number="CO-001",
        name="prod",
        api_url="https://x",
        ca_bundle="-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n",
        openbao_mount="kubernetes/tenant-x",
    )
    CreateClusterRequest(slug="acme-prod", **base)
    CreateClusterRequest(slug="a", **base)
    with pytest.raises(ValidationError):
        CreateClusterRequest(slug="-leading", **base)
    with pytest.raises(ValidationError):
        CreateClusterRequest(slug="UPPER", **base)
    with pytest.raises(ValidationError):
        CreateClusterRequest(slug="trailing-", **base)


def test_create_cluster_request_worker_groups_min() -> None:
    base = dict(
        contract_number="CO-001",
        name="prod",
        slug="acme",
        api_url="https://x",
        ca_bundle="-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n",
        openbao_mount="kubernetes/tenant-x",
    )
    CreateClusterRequest(worker_groups=1, **base)
    with pytest.raises(ValidationError):
        CreateClusterRequest(worker_groups=0, **base)
