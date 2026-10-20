"""Validation of upstream URLs registered by administrators.

Clients never choose a destination; only admins register upstreams. This module is the
second line of defence: it stops an admin mistake (or a stolen admin token) from pointing
the gateway at addresses it should never call, such as the cloud metadata service.

Limitation, stated honestly: this checks the URL as written. A hostname can still resolve
to a bad address (or be changed to later, "DNS rebinding"). Production hardening is
network-level egress rules plus resolving and checking the address at connect time.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from app.core.config import get_settings

BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata.goog", "instance-data"}
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$", re.IGNORECASE)
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Legacy forms such as 2852039166 or 0xa9fea9fe are not valid to ipaddress, but
    # resolvers (inet_aton) treat them as IPv4 addresses, so we must too.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise ValueError("Upstream address is not allowed (link-local, unspecified or reserved)")
    if isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK:
        raise ValueError("Upstream address is not allowed (0.0.0.0/8)")
    if not get_settings().allow_private_upstreams and (ip.is_private or ip.is_loopback):
        raise ValueError("Private and loopback upstream addresses are disabled")


def validate_upstream_url(raw: str) -> str:
    """Return a normalised base URL (no trailing slash) or raise ValueError."""
    raw = raw.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = parts.hostname
    if not host:
        raise ValueError("URL must include a host")
    if "@" in parts.netloc:
        raise ValueError("URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("URL must not contain a query string or fragment")
    if "\\" in raw or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise ValueError("URL contains invalid characters")
    if port is not None and port == 0:
        raise ValueError("Invalid port")

    host = host.lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES:
        raise ValueError("Upstream hostname is not allowed")
    ip = _as_ip(host)
    if ip is not None:
        _check_ip(ip)
    elif ":" not in host and not _HOSTNAME_RE.match(host):
        raise ValueError("Invalid hostname")

    path = parts.path.rstrip("/")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("URL path must not contain '.' or '..' segments")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
