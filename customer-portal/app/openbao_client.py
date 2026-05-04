"""Async OpenBao client used by the portal.

The portal authenticates to OpenBao with its in-cluster ServiceAccount JWT
(Kubernetes auth method), then uses that token to fetch ephemeral
ServiceAccount credentials from per-tenant `kubernetes` secrets-engine mounts.
"""

import asyncio
import logging
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OpenBaoError(RuntimeError):
    pass


class OpenBaoClient:
    """Process-wide OpenBao client. One instance is created at app startup."""

    def __init__(self, settings: Settings):
        self._addr = settings.openbao_addr.rstrip("/")
        self._role = settings.openbao_k8s_auth_role
        self._sa_token_path = settings.openbao_sa_token_path
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        # OPENBAO_CA_PATH overrides the system trust store for the OpenBao
        # endpoint; useful when OpenBao is fronted by an internal CA.
        verify: str | bool = settings.openbao_ca_path or True
        # Reusable client; OpenBao is internal, no proxy needed.
        self._http = httpx.AsyncClient(timeout=10.0, verify=verify, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> str:
        try:
            with open(self._sa_token_path, "r") as f:
                jwt = f.read().strip()
        except OSError as exc:
            raise OpenBaoError(
                f"Cannot read SA token at {self._sa_token_path}: {exc}"
            ) from exc

        resp = await self._http.post(
            f"{self._addr}/v1/auth/kubernetes/login",
            json={"role": self._role, "jwt": jwt},
        )
        if resp.status_code != 200:
            logger.error(
                "OpenBao kubernetes login failed: status=%s body=%s",
                resp.status_code, resp.text[:500],
            )
            raise OpenBaoError(
                f"OpenBao kubernetes login failed (status {resp.status_code})"
            )
        body = resp.json()
        auth = body.get("auth") or {}
        token = auth.get("client_token")
        lease = auth.get("lease_duration", 0)
        if not token:
            logger.error("OpenBao login response missing client_token (keys=%s)", list(body))
            raise OpenBaoError("OpenBao login response missing client_token")
        # Renew slightly before actual expiry.
        self._token = token
        self._token_expires_at = time.monotonic() + max(60, int(lease) - 30)
        logger.info("Logged in to OpenBao at %s as role %s", self._addr, self._role)
        return token

    async def _token_or_login(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            return await self._login()

    async def get_k8s_creds(
        self, mount: str, role: str, *, kubernetes_namespace: str = "kube-system"
    ) -> dict:
        """Mint a fresh ephemeral SA token at /v1/<mount>/creds/<role>.

        The kubernetes secrets engine generates credentials via a POST (it
        accepts optional override parameters in the body); a GET returns
        405 "unsupported operation". Caller is responsible for not logging
        the returned token.

        `kubernetes_namespace` is the namespace the OpenBao-bound SA lives in
        (per our bootstrap convention, `kube-system`). Required when the role
        has more than one entry in `allowed_kubernetes_namespaces`; harmless
        when it has exactly one.

        Returns the OpenBao `data` block, which for this engine includes at
        minimum `service_account_token`.
        """
        token = await self._token_or_login()
        path = f"{self._addr}/v1/{mount.strip('/')}/creds/{role}"
        body_payload = {"kubernetes_namespace": kubernetes_namespace}
        resp = await self._http.post(
            path, headers={"X-Vault-Token": token}, json=body_payload
        )
        if resp.status_code == 403:
            # Token may have expired; re-login once and retry.
            self._token = None
            token = await self._token_or_login()
            resp = await self._http.post(
                path, headers={"X-Vault-Token": token}, json=body_payload
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "OpenBao mint %s/creds/%s failed: status=%s body=%s",
                mount, role, resp.status_code, resp.text[:500],
            )
            raise OpenBaoError(
                f"OpenBao mint {mount}/creds/{role} failed (status {resp.status_code})"
            )
        body = resp.json()
        data = body.get("data")
        if not data or "service_account_token" not in data:
            logger.error(
                "OpenBao response missing service_account_token (keys=%s)",
                list((data or {}).keys()),
            )
            raise OpenBaoError("OpenBao response missing service_account_token")
        return data


_client: OpenBaoClient | None = None


def init_openbao(settings: Settings) -> None:
    global _client
    _client = OpenBaoClient(settings)


def get_openbao() -> OpenBaoClient:
    if _client is None:
        raise RuntimeError("OpenBao client not initialized")
    return _client


async def shutdown_openbao() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
