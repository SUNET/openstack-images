"""Per-request K8s client for a tenant cluster.

Each `TenantClusterClient` instance fetches a fresh, short-lived ServiceAccount
token from OpenBao's `kubernetes` secrets engine, builds a kubernetes-client
ApiClient against the tenant cluster's API URL + CA, and exposes the small
set of operations the portal needs (CSR submit/approve, RoleBinding CRUD).

The token is *not* persisted; it expires on its own ~60s after this client is
constructed. `aclose()` writes nothing back to the cluster — there's nothing
to clean up that K8s won't reap on TTL.
"""

import asyncio
import base64
import logging
import os
import tempfile
import time

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from app.models import TenantCluster
from app.openbao_client import get_openbao

logger = logging.getLogger(__name__)


# K8s built-in signer that signs client certs with the cluster CA via
# kube-controller-manager.
KUBE_APISERVER_CLIENT_SIGNER = "kubernetes.io/kube-apiserver-client"


class TenantClusterError(RuntimeError):
    pass


class TenantClusterClient:
    """Async context manager wrapping a kubernetes-client ApiClient.

    Usage:
        async with TenantClusterClient(cluster) as tc:
            cert_pem = await tc.submit_csr(csr_pem, ttl_days=365)
            await tc.create_rolebinding(...)
    """

    def __init__(self, cluster: TenantCluster):
        self._cluster = cluster
        self._ca_file: str | None = None
        self._api: k8s_client.ApiClient | None = None

    async def __aenter__(self) -> "TenantClusterClient":
        bao = get_openbao()
        creds = await bao.get_k8s_creds(self._cluster.openbao_mount, self._cluster.openbao_role)
        token = creds["service_account_token"]

        # The kubernetes Python client does blocking file IO and TLS on socket
        # operations. We're fine running it from async code because everything
        # we do is short — but we materialise the CA bundle to a tempfile
        # because the client wants a path, not a string.
        fd, path = tempfile.mkstemp(prefix="tenant-ca-", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(self._cluster.ca_bundle)
        self._ca_file = path

        cfg = k8s_client.Configuration()
        cfg.host = self._cluster.api_url
        cfg.api_key = {"authorization": f"Bearer {token}"}
        cfg.ssl_ca_cert = path
        cfg.verify_ssl = True
        self._api = k8s_client.ApiClient(cfg)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None
        if self._ca_file is not None:
            try:
                os.unlink(self._ca_file)
            except OSError:
                pass
            self._ca_file = None

    @property
    def api(self) -> k8s_client.ApiClient:
        if self._api is None:
            raise RuntimeError("TenantClusterClient used outside `async with`")
        return self._api

    # --- CSR submit + approve + read ---

    async def submit_csr(
        self,
        csr_pem: str,
        *,
        username: str,
        ttl_seconds: int,
        signer_name: str = KUBE_APISERVER_CLIENT_SIGNER,
    ) -> tuple[str, str]:
        """Create + approve a CSR; poll for the signed cert.

        Returns `(name, signed_cert_pem)`. Raises TenantClusterError on timeout.
        """
        certs = k8s_client.CertificatesV1Api(self.api)
        # K8s 1.22+ requires expirationSeconds on the spec for non-default TTL.
        body = k8s_client.V1CertificateSigningRequest(
            api_version="certificates.k8s.io/v1",
            kind="CertificateSigningRequest",
            metadata=k8s_client.V1ObjectMeta(generate_name="portal-issued-"),
            spec=k8s_client.V1CertificateSigningRequestSpec(
                request=base64.b64encode(csr_pem.encode()).decode(),
                signer_name=signer_name,
                usages=["digital signature", "key encipherment", "client auth"],
                expiration_seconds=ttl_seconds,
                username=username,
            ),
        )
        loop = asyncio.get_event_loop()
        created = await loop.run_in_executor(
            None, lambda: certs.create_certificate_signing_request(body)
        )
        name = created.metadata.name

        # Approve.
        approval = k8s_client.V1CertificateSigningRequest(
            api_version="certificates.k8s.io/v1",
            kind="CertificateSigningRequest",
            metadata=created.metadata,
            spec=created.spec,
            status=k8s_client.V1CertificateSigningRequestStatus(
                conditions=[
                    k8s_client.V1CertificateSigningRequestCondition(
                        type="Approved",
                        status="True",
                        reason="PortalAutoApprove",
                        message="Approved by customer-portal",
                    )
                ]
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: certs.replace_certificate_signing_request_approval(name, approval),
        )

        # Poll for the signed cert.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            cur = await loop.run_in_executor(
                None, lambda: certs.read_certificate_signing_request(name)
            )
            if cur.status and cur.status.certificate:
                pem = base64.b64decode(cur.status.certificate).decode()
                return name, pem
            await asyncio.sleep(0.5)

        raise TenantClusterError(f"CSR {name} not signed within timeout")

    async def delete_csr(self, name: str) -> None:
        """Best-effort cleanup; CSRs are GC'd by K8s but we can be tidy."""
        certs = k8s_client.CertificatesV1Api(self.api)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: certs.delete_certificate_signing_request(name)
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete CSR %s: %s", name, e)

    # --- RoleBinding CRUD ---

    async def create_rolebinding(
        self,
        *,
        name: str,
        namespace: str,
        role_name: str,
        group: str,
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> None:
        rbac = k8s_client.RbacAuthorizationV1Api(self.api)
        body = k8s_client.V1RoleBinding(
            metadata=k8s_client.V1ObjectMeta(
                name=name, namespace=namespace,
                labels=labels, annotations=annotations or None,
            ),
            role_ref=k8s_client.V1RoleRef(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=role_name,
            ),
            subjects=[
                k8s_client.RbacV1Subject(
                    api_group="rbac.authorization.k8s.io",
                    kind="Group",
                    name=group,
                )
            ],
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: rbac.create_namespaced_role_binding(namespace, body)
        )

    async def delete_rolebinding(self, *, name: str, namespace: str) -> bool:
        rbac = k8s_client.RbacAuthorizationV1Api(self.api)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: rbac.delete_namespaced_role_binding(name, namespace)
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def list_rolebinding_names_by_label(
        self, *, namespace: str, label_selector: str
    ) -> list[str]:
        rbac = k8s_client.RbacAuthorizationV1Api(self.api)
        loop = asyncio.get_event_loop()
        rbs = await loop.run_in_executor(
            None,
            lambda: rbac.list_namespaced_role_binding(
                namespace, label_selector=label_selector
            ),
        )
        return [rb.metadata.name for rb in rbs.items]
