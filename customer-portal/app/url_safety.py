"""URL safety helpers for outbound deliveries (anti-SSRF)."""

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeDeliveryURL(ValueError):
    """Raised when a user-supplied delivery URL fails safety validation."""


def _is_private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        # Cloud metadata endpoints (AWS/GCP/Azure all use 169.254.169.254
        # which is_link_local catches; this also blocks the v6 fc00::/7
        # ULA range that is_private already covers — safe to leave).
    )


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Match `host` against the allowlist (exact or ``*.`` wildcard).

    A ``*.suffix`` entry matches any subdomain of ``suffix`` on a label
    boundary (``su.drive.sunet.se`` matches ``*.drive.sunet.se``) but not
    the bare apex and not lookalikes (``evildrive.sunet.se`` does not).
    """
    for entry in allowed_hosts:
        entry = entry.lower()
        if entry.startswith("*."):
            if host.endswith(entry[1:]):  # entry[1:] keeps the leading dot
                return True
        elif host == entry:
            return True
    return False


def validate_webdav_url(url: str, allowed_hosts: list[str]) -> None:
    """Reject WebDAV URLs that are unsafe to PUT to from the portal pod.

    Rules:
      - Scheme must be https.
      - Hostname must match `allowed_hosts` (exact or ``*.`` wildcard,
        case-insensitive).
      - All resolved IPs must be public (no RFC1918/loopback/link-local/
        metadata).

    Empty `allowed_hosts` rejects everything — the portal admin must opt in
    to specific WebDAV destinations.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeDeliveryURL("WebDAV URL must use https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeDeliveryURL("WebDAV URL is missing a hostname")
    if not _host_allowed(host, allowed_hosts):
        raise UnsafeDeliveryURL(
            "WebDAV host is not in the portal's WEBDAV_ALLOWED_HOSTS allowlist"
        )
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeDeliveryURL(f"WebDAV host could not be resolved: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        if _is_private_ip(addr):
            raise UnsafeDeliveryURL(
                "WebDAV host resolves to a private/loopback/link-local address"
            )
