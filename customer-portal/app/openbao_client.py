"""Async OpenBao client used by the portal.

The portal authenticates to OpenBao with its in-cluster ServiceAccount JWT
(Kubernetes auth method), then uses that token to fetch ephemeral
ServiceAccount credentials from per-tenant `kubernetes` secrets-engine mounts.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OpenBaoError(RuntimeError):
    """A sanitized upstream failure, safe to include in a portal error."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OpenBaoCASConflict(OpenBaoError):
    """The KV version changed before a conditional write could complete."""


@dataclass(frozen=True)
class VersionedKVSecret:
    data: dict[str, str] = field(repr=False)
    version: int


_REPOSITORY_KV_PATH = re.compile(
    r"kv/data/customer-cluster-repositories/[1-9][0-9]*/(?:test|prod)/(?:writer|reader)"
)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Never expose an upstream body, including malformed JSON, in an exception."""
    try:
        body = response.json()
    except ValueError:
        raise OpenBaoError("OpenBao returned an invalid response") from None
    if not isinstance(body, dict):
        raise OpenBaoError("OpenBao returned an invalid response")
    return body


def _kv_path(path: str) -> str:
    if not _REPOSITORY_KV_PATH.fullmatch(path):
        raise OpenBaoError("Invalid repository secret path")
    return path


def _version(value: object, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise OpenBaoError("OpenBao secret version is invalid")
    return value


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
        self._http = httpx.AsyncClient(
            timeout=10.0, verify=verify, follow_redirects=False, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> str:
        try:
            jwt = Path(self._sa_token_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise OpenBaoError("Cannot read OpenBao service account token") from None
        if not jwt:
            raise OpenBaoError("OpenBao service account token is empty")

        try:
            resp = await self._http.post(
                f"{self._addr}/v1/auth/kubernetes/login",
                json={"role": self._role, "jwt": jwt},
            )
        except httpx.RequestError:
            raise OpenBaoError("OpenBao authentication is unavailable") from None
        if resp.status_code != 200:
            raise OpenBaoError(
                f"OpenBao kubernetes login failed (status {resp.status_code})",
                status_code=resp.status_code,
            )
        auth = _json_object(resp).get("auth")
        if not isinstance(auth, dict):
            raise OpenBaoError("OpenBao login response is invalid")
        token = auth.get("client_token")
        lease = auth.get("lease_duration")
        if not isinstance(token, str) or not token or any(
            ord(char) < 33 or ord(char) > 126 for char in token
        ):
            raise OpenBaoError("OpenBao login response missing client_token")
        if type(lease) is not int or lease < 0:
            raise OpenBaoError("OpenBao login response has an invalid lease")
        # Short leases must never be cached beyond their actual expiry.
        self._token = token
        self._token_expires_at = time.monotonic() + max(0, lease - min(30, lease / 10))
        logger.info("Logged in to OpenBao at %s as role %s", self._addr, self._role)
        return token

    async def _token_or_login(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            return await self._login()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        version: int | None = None,
    ) -> httpx.Response:
        """Retry only an explicit authentication rejection, and at most once."""
        token = await self._token_or_login()
        for attempt in range(2):
            try:
                response = await self._http.request(
                    method,
                    f"{self._addr}/v1/{path}",
                    headers={"X-Vault-Token": token},
                    json=payload,
                    params={"version": version} if version is not None else None,
                )
            except httpx.RequestError:
                # A timed-out write may already have succeeded; do not replay it.
                raise OpenBaoError("OpenBao request failed") from None
            if response.status_code not in (401, 403) or attempt == 1:
                return response
            async with self._lock:
                # Another request may already have replaced the rejected token.
                if self._token == token:
                    self._token = None
                if self._token and time.monotonic() < self._token_expires_at:
                    token = self._token
                else:
                    token = await self._login()
        raise AssertionError("Unreachable OpenBao retry state")

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
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", mount) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", role
        ):
            raise OpenBaoError("Invalid Kubernetes credential path")
        path = f"{mount}/creds/{role}"
        body_payload = {"kubernetes_namespace": kubernetes_namespace}
        resp = await self._request("POST", path, payload=body_payload)
        if resp.status_code not in (200, 201):
            raise OpenBaoError(
                f"OpenBao credential mint failed (status {resp.status_code})",
                status_code=resp.status_code,
            )
        data = _json_object(resp).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("service_account_token"), str):
            raise OpenBaoError("OpenBao response missing service_account_token")
        if not data["service_account_token"]:
            raise OpenBaoError("OpenBao response missing service_account_token")
        return data

    async def write_kv_secret(self, path: str, data: dict[str, str], *, cas: int) -> int:
        """Conditionally append a KV v2 version; zero means create only."""
        clean_path = _kv_path(path)
        _version(cas, allow_zero=True)
        if not isinstance(data, dict) or not data or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise OpenBaoError("Invalid repository secret data")
        response = await self._request(
            "POST", clean_path, payload={"data": data, "options": {"cas": cas}}
        )
        if response.status_code == 400:
            errors = _json_object(response).get("errors")
            if isinstance(errors, list) and any(
                isinstance(error, str)
                and "check-and-set parameter did not match the current version" in error
                for error in errors
            ):
                raise OpenBaoCASConflict("OpenBao secret version conflict", status_code=400)
        if response.status_code not in (200, 201):
            raise OpenBaoError(
                f"OpenBao secret write failed (status {response.status_code})",
                status_code=response.status_code,
            )
        metadata = _json_object(response).get("data")
        if not isinstance(metadata, dict):
            raise OpenBaoError("OpenBao secret write response is invalid")
        version = _version(metadata.get("version"))
        if version != cas + 1:
            raise OpenBaoError("OpenBao secret write returned an unexpected version")
        return version

    async def read_kv_secret_versioned(
        self, path: str, *, version: int | None = None
    ) -> VersionedKVSecret:
        """Read a pinned version, or latest only for explicit legacy adoption."""
        clean_path = _kv_path(path)
        if version is not None:
            _version(version)
        response = await self._request("GET", clean_path, version=version)
        if response.status_code != 200:
            raise OpenBaoError(
                f"OpenBao secret read failed (status {response.status_code})",
                status_code=response.status_code,
            )
        data = _json_object(response).get("data")
        if not isinstance(data, dict):
            raise OpenBaoError("OpenBao secret has an invalid shape")
        payload, metadata = data.get("data"), data.get("metadata")
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ) or not isinstance(metadata, dict):
            raise OpenBaoError("OpenBao secret has an invalid shape")
        actual_version = _version(metadata.get("version"))
        if version is not None and actual_version != version:
            raise OpenBaoError("OpenBao returned a different secret version")
        return VersionedKVSecret(data=payload, version=actual_version)

    async def read_kv_secret(self, path: str, *, version: int | None = None) -> dict[str, str]:
        """Read only the secret data; callers must not log or return these values."""
        secret = await self.read_kv_secret_versioned(
            path, version=version
        )
        return secret.data


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
