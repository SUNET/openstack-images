"""SSE-KMS transit key management in OpenBao.

Each Keystone project gets a dedicated transit key named
``kms-<project-id>`` so customers can use it as their SSE-KMS key id
(``--sse aws:kms --sse-kms-key-id kms-<project-id>``) when uploading
to RGW.

Auth: the operator's pod runs alongside an injected Vault agent
sidecar that authenticates via the Kubernetes auth method (Vault
role ``openstack-operator``) and writes a token to
``/vault/secrets/token``. ``BAO_ADDR`` selects the OpenBao endpoint;
if it is unset the module is a no-op so the operator can run in
environments without OpenBao.
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_FILE = "/vault/secrets/token"
_REQUEST_TIMEOUT = 10


def kms_key_name(project_id: str) -> str:
    """Return the conventional SSE-KMS key name for a project."""
    return f"kms-{project_id}"


def _bao_config() -> tuple[str, str] | None:
    """Return ``(addr, token)`` if OpenBao is configured, else ``None``."""
    addr = os.environ.get("BAO_ADDR")
    if not addr:
        return None
    token_file = os.environ.get("BAO_TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    try:
        with open(token_file) as f:
            token = f.read().strip()
    except OSError as e:
        logger.warning("BAO_ADDR set but token file %s unreadable: %s", token_file, e)
        return None
    if not token:
        logger.warning("Bao token file %s is empty", token_file)
        return None
    return addr, token


def _bao_request(
    method: str, path: str, body: dict[str, str] | None = None
) -> tuple[int, bytes]:
    """Issue a request to OpenBao. Returns ``(status_code, body_bytes)``."""
    cfg = _bao_config()
    if cfg is None:
        raise RuntimeError("OpenBao not configured (BAO_ADDR unset)")
    addr, token = cfg
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{addr}/v1{path}",
        data=data,
        method=method,
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def ensure_transit_key(project_id: str) -> bool:
    """Ensure the SSE-KMS transit key for a project exists in OpenBao.

    Returns ``True`` if the key exists or was created, ``False`` if
    OpenBao is not configured (no-op). Raises on unexpected errors.
    """
    if _bao_config() is None:
        logger.debug(
            "BAO_ADDR unset; skipping SSE-KMS key provisioning for project %s",
            project_id,
        )
        return False

    name = kms_key_name(project_id)
    code, body = _bao_request("GET", f"/transit/keys/{name}")
    if code == 200:
        logger.info("SSE-KMS transit key %s already exists", name)
        return True
    if code != 404:
        raise RuntimeError(f"Bao GET transit/keys/{name} returned {code}: {body!r}")

    code, body = _bao_request(
        "POST", f"/transit/keys/{name}", {"type": "aes256-gcm96"}
    )
    if code not in (200, 204):
        raise RuntimeError(
            f"Bao create transit/keys/{name} returned {code}: {body!r}"
        )
    logger.info("Created SSE-KMS transit key %s", name)
    return True
