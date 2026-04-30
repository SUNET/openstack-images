"""Issue, rotate, and revoke per-issuance tenant cluster kubeconfigs.

Each issuance:
  - generates a fresh keypair locally,
  - submits a CSR (CN=oidc:<sub>, O=tenant-cluster-<slug>-issuance-<uuid>) to
    the tenant cluster's K8s built-in `kubernetes.io/kube-apiserver-client`
    signer,
  - creates a labeled RoleBinding in the cluster's `argocd` namespace mapping
    the cert's `O` group to the pre-installed `argocd-tenant` Role,
  - returns a kubeconfig YAML to the user.

Revocation deletes the RoleBinding. The cert can still authenticate (K8s has
no CRL) but has no permissions — equivalent to revoked. Cascade-on-access-
removal lists+deletes all RoleBindings labeled with the user's `oidc-sub`.
"""

import base64
import hashlib
import logging
import uuid
from datetime import datetime, timezone

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cluster_client import TenantClusterClient
from app.models import KubeconfigIssuance, TenantCluster

logger = logging.getLogger(__name__)

# Filterable label: a hex hash of the OIDC sub, because K8s label values
# cannot contain '@' or '/' (a typical OIDC sub like "kano@sunet.se" fails
# K8s label validation). The annotation carries the raw sub for human
# readability and audit; cascade-revoke filters by the hashed label.
LABEL_OIDC_SUB_HASH = "sunet.se/oidc-sub-hash"
LABEL_ISSUANCE_ID = "sunet.se/issuance-id"
ANNOTATION_OIDC_SUB = "sunet.se/oidc-sub"


def oidc_sub_label_hash(user_sub: str) -> str:
    """Stable, label-safe identifier for an OIDC sub (hex sha256, first 32)."""
    return hashlib.sha256(user_sub.encode("utf-8")).hexdigest()[:32]


def _build_kubeconfig(
    *, cluster_name: str, api_url: str, ca_bundle: str,
    cert_pem: str, key_pem: str, user_name: str,
) -> str:
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": cluster_name,
                "cluster": {
                    "server": api_url,
                    "certificate-authority-data": base64.b64encode(
                        ca_bundle.encode()
                    ).decode(),
                },
            }
        ],
        "users": [
            {
                "name": user_name,
                "user": {
                    "client-certificate-data": base64.b64encode(cert_pem.encode()).decode(),
                    "client-key-data": base64.b64encode(key_pem.encode()).decode(),
                },
            }
        ],
        "contexts": [
            {
                "name": cluster_name,
                "context": {
                    "cluster": cluster_name,
                    "user": user_name,
                    "namespace": "argocd",
                },
            }
        ],
        "current-context": cluster_name,
    }
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def _build_csr(*, common_name: str, organization: str) -> tuple[str, str]:
    """Generate keypair + CSR. Returns (csr_pem, key_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
                ]
            )
        )
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return csr_pem, key_pem


def _cert_serial_and_expiry(cert_pem: str) -> tuple[str, datetime]:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    serial_hex = format(cert.serial_number, "x")
    return serial_hex, cert.not_valid_after_utc.replace(tzinfo=None)


async def issue(
    cluster: TenantCluster,
    *,
    user_sub: str,
    label: str,
    ttl_days: int,
    session: AsyncSession,
) -> tuple[KubeconfigIssuance, str]:
    """Issue a fresh kubeconfig for `user_sub` on `cluster`."""
    issuance_id = uuid.uuid4().hex
    organization = f"tenant-cluster-{cluster.slug}-issuance-{issuance_id}"
    common_name = f"oidc:{user_sub}"
    rolebinding_name = f"portal-{issuance_id}"

    csr_pem, key_pem = _build_csr(
        common_name=common_name, organization=organization
    )

    ttl_seconds = ttl_days * 24 * 3600
    async with TenantClusterClient(cluster) as tc:
        csr_name, cert_pem = await tc.submit_csr(
            csr_pem,
            username=common_name,
            ttl_seconds=ttl_seconds,
        )
        try:
            await tc.create_rolebinding(
                name=rolebinding_name,
                namespace=cluster.argocd_namespace,
                role_name=cluster.argocd_role_name,
                group=organization,
                labels={
                    LABEL_OIDC_SUB_HASH: oidc_sub_label_hash(user_sub),
                    LABEL_ISSUANCE_ID: issuance_id,
                },
                annotations={
                    ANNOTATION_OIDC_SUB: user_sub,
                },
            )
        except Exception:
            # Best-effort cleanup of the now-orphan CSR; cert is harmless on
            # its own without an RBAC binding, but tidiness matters.
            await tc.delete_csr(csr_name)
            raise
        # CSR objects are not needed once we've extracted the cert.
        await tc.delete_csr(csr_name)

    serial, expires_at = _cert_serial_and_expiry(cert_pem)
    issuance = KubeconfigIssuance(
        cluster_id=cluster.id,
        user_sub=user_sub,
        label=label,
        cert_serial=serial,
        rolebinding_name=rolebinding_name,
        cert_group=organization,
        expires_at=expires_at,
    )
    session.add(issuance)
    await session.flush()

    kubeconfig_yaml = _build_kubeconfig(
        cluster_name=cluster.slug,
        api_url=cluster.api_url,
        ca_bundle=cluster.ca_bundle,
        cert_pem=cert_pem,
        key_pem=key_pem,
        user_name=common_name,
    )
    return issuance, kubeconfig_yaml


async def revoke(
    cluster: TenantCluster,
    issuance: KubeconfigIssuance,
    *,
    by_sub: str,
    session: AsyncSession,
) -> None:
    if issuance.revoked_at is not None:
        return
    async with TenantClusterClient(cluster) as tc:
        await tc.delete_rolebinding(
            name=issuance.rolebinding_name,
            namespace=cluster.argocd_namespace,
        )
    issuance.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    issuance.revoked_by_sub = by_sub
    await session.flush()


async def cascade_revoke_for_user(
    cluster: TenantCluster,
    *,
    user_sub: str,
    by_sub: str,
    session: AsyncSession,
) -> int:
    """Revoke every active issuance for `user_sub` on `cluster`."""
    label_selector = f"{LABEL_OIDC_SUB_HASH}={oidc_sub_label_hash(user_sub)}"
    async with TenantClusterClient(cluster) as tc:
        names = await tc.list_rolebinding_names_by_label(
            namespace=cluster.argocd_namespace,
            label_selector=label_selector,
        )
        for name in names:
            await tc.delete_rolebinding(
                name=name, namespace=cluster.argocd_namespace
            )

    rows = (
        await session.execute(
            select(KubeconfigIssuance).where(
                KubeconfigIssuance.cluster_id == cluster.id,
                KubeconfigIssuance.user_sub == user_sub,
                KubeconfigIssuance.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        row.revoked_at = now
        row.revoked_by_sub = by_sub
    await session.flush()
    return len(rows)


def issuance_status(issuance: KubeconfigIssuance) -> str:
    if issuance.revoked_at is not None:
        return "revoked"
    if issuance.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return "expired"
    return "active"


def default_ttl_days_or(value: int | None, default: int) -> int:
    if value is None:
        return default
    return value


__all__ = [
    "issue",
    "revoke",
    "cascade_revoke_for_user",
    "issuance_status",
    "default_ttl_days_or",
    "oidc_sub_label_hash",
    "LABEL_OIDC_SUB_HASH",
    "LABEL_ISSUANCE_ID",
    "ANNOTATION_OIDC_SUB",
]
