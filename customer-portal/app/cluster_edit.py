"""Validation and serialization for portal cluster connection edits."""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import KubeconfigIssuance, TenantCluster

_CERTIFICATE_PEM = re.compile(
    r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----"
)


def validate_api_url(value: str) -> str:
    """Accept only a canonical HTTPS Kubernetes API origin, without URL extras."""
    message = "API URL must be an HTTPS hostname on port 6443 without credentials or URL extras"
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.port != 6443
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
            or value != f"https://{parsed.hostname}:6443"
        ):
            raise ValueError(message)
    except ValueError as exc:
        raise ValueError(message) from exc
    return value


def validate_ca_bundle(value: str) -> str:
    """Parse a certificate-only PEM bundle and require currently valid CA certificates."""
    message = "CA bundle must contain only valid PEM CA certificates, without private keys"
    if not value.strip() or _CERTIFICATE_PEM.sub("", value).strip():
        raise ValueError(message)
    try:
        certificates = x509.load_pem_x509_certificates(value.encode("ascii"))
        now = datetime.now(UTC)
        for certificate in certificates:
            if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError("CA bundle cannot contain client or server leaf certificates")
            if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
                raise ValueError("CA bundle contains an expired or not-yet-valid certificate")
            try:
                usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
            except x509.ExtensionNotFound:
                pass
            else:
                if not usage.key_cert_sign:
                    raise ValueError("CA certificate key usage must permit certificate signing")
    except (
        ValueError, UnicodeError, UnsupportedAlgorithm, x509.ExtensionNotFound,
        x509.DuplicateExtension,
    ) as exc:
        raise ValueError(message) from exc
    return value


async def locked_cluster(slug: str, session: AsyncSession) -> TenantCluster:
    """Re-read metadata after waiting for any earlier cluster mutation to commit."""
    cluster = await session.scalar(
        select(TenantCluster)
        .where(TenantCluster.slug == slug)
        .with_for_update()
        .execution_options(populate_existing=True)
        .options(selectinload(TenantCluster.contract))
    )
    if cluster is None:
        raise HTTPException(404, "Cluster not found")
    return cluster


async def require_connection_change_allowed(
    cluster: TenantCluster,
    api_url: str,
    ca_bundle: str,
    session: AsyncSession,
) -> None:
    """Keep issued kubeconfigs usable; connection migration must be coordinated manually."""
    if (api_url, ca_bundle) == (cluster.api_url, cluster.ca_bundle):
        return
    issuance = await session.scalar(
        select(KubeconfigIssuance.id)
        .where(
            KubeconfigIssuance.cluster_id == cluster.id,
            KubeconfigIssuance.revoked_at.is_(None),
            KubeconfigIssuance.expires_at > datetime.now(UTC).replace(tzinfo=None),
        )
        .limit(1)
    )
    if issuance is not None:
        raise HTTPException(
            409,
            "Cluster has active issued credentials; API endpoint or CA changes require "
            "a coordinated manual migration",
        )
