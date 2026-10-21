"""SSRF guard (BLUEPRINT.md §9). Validates a URL before we hand it to a
scraper — defense in depth even though Firecrawl does the actual fetch on
its own infrastructure, not ours: it's still our code deciding what's
worth asking Firecrawl to fetch in the first place."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from core.errors import GuardrailViolation

ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified


def assert_safe_url(url: str) -> None:
    """Raises GuardrailViolation if `url` is not a safe http(s) URL that
    doesn't resolve to a private/loopback/link-local/metadata address."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise GuardrailViolation(f"unsafe URL scheme: {url!r}")
    if not parsed.hostname:
        raise GuardrailViolation(f"unsafe URL (no host): {url!r}")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise GuardrailViolation(f"could not resolve host for {url!r}: {exc}") from exc

    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise GuardrailViolation(f"unsafe URL (resolves to blocked address {ip}): {url!r}")


def is_safe_url(url: str) -> bool:
    try:
        assert_safe_url(url)
        return True
    except GuardrailViolation:
        return False
