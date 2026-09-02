"""Configure Git authentication without putting secrets in repository URLs."""

import base64
from urllib.parse import urlsplit


def git_auth_environment(repo_url: str, username: str, token: str) -> dict[str, str]:
    """Return process-scoped Git configuration for HTTP basic authentication."""
    parsed = urlsplit(repo_url)
    if parsed.scheme not in ("http", "https"):
        return {"GIT_TERMINAL_PROMPT": "0"}
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Git repository URLs must not contain credentials")
    if not parsed.hostname:
        raise RuntimeError(f"Git repository URL is invalid (got {repo_url!r})")
    if not username or not token:
        raise RuntimeError("Git username and token must be configured")
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{origin}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        "GIT_TERMINAL_PROMPT": "0",
    }
