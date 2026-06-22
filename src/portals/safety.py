from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit


class PortalUrlSafetyError(ValueError):
    """Raised when an admin-supplied URL is unsafe for the crawler."""


@dataclass(frozen=True)
class ValidatedPortalUrl:
    original_url: str
    normalized_url: str
    hostname: str
    allowed_hosts: tuple[str, ...]


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().rstrip(".").lower()
    if not host:
        raise PortalUrlSafetyError("URL must include a hostname.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PortalUrlSafetyError("URL hostname is invalid.") from exc


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


@lru_cache(maxsize=4096)
def _resolve_public_addresses(hostname: str) -> tuple[str, ...]:
    """Resolve a host once per worker and reject mixed/public-private answers.

    Rejecting the entire host when any resolved address is private avoids a common
    SSRF bypass where one DNS answer is public and another answer is internal.
    """
    try:
        rows = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PortalUrlSafetyError("Hostname could not be resolved.") from exc

    addresses = tuple(sorted({str(row[4][0]) for row in rows if row and row[4]}))
    if not addresses:
        raise PortalUrlSafetyError("Hostname did not resolve to an address.")
    if not all(_is_public_ip(address) for address in addresses):
        raise PortalUrlSafetyError("URL resolves to a private, reserved, or loopback address.")
    return addresses


def _validate_host_is_public(hostname: str) -> None:
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise PortalUrlSafetyError("Localhost URLs are not allowed.")

    try:
        parsed_ip = ipaddress.ip_address(hostname)
    except ValueError:
        _resolve_public_addresses(hostname)
        return

    if not parsed_ip.is_global:
        raise PortalUrlSafetyError("Private, loopback, and reserved IP addresses are not allowed.")


def _normalize_url_parts(parts: SplitResult, hostname: str) -> str:
    scheme = parts.scheme.lower()
    port = parts.port
    if port and port not in {80, 443}:
        raise PortalUrlSafetyError("Only standard HTTP and HTTPS ports are allowed.")

    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def normalize_allowed_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for host in hosts:
        candidate = _normalize_hostname(str(host).lstrip("*.") if str(host).startswith("*.") else str(host))
        value = f"*.{candidate}" if str(host).startswith("*.") else candidate
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def host_is_allowed(hostname: str, allowed_hosts: Iterable[str]) -> bool:
    host = _normalize_hostname(hostname)
    for configured in normalize_allowed_hosts(allowed_hosts):
        if configured.startswith("*."):
            suffix = configured[1:]  # includes leading dot
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == configured:
            return True
    return False


def default_allowed_hosts(hostname: str) -> tuple[str, ...]:
    """Allow only the input host and its conventional www sibling.

    Cross-domain ATS links must be added through a reviewed configuration update;
    the initial create endpoint never silently expands the crawler allow-list.
    """
    host = _normalize_hostname(hostname)
    sibling = host[4:] if host.startswith("www.") else f"www.{host}"
    return tuple(dict.fromkeys((host, sibling)))


def validate_public_http_url(raw_url: str, *, allowed_hosts: Iterable[str] | None = None) -> ValidatedPortalUrl:
    value = str(raw_url or "").strip()
    if not value:
        raise PortalUrlSafetyError("A job-listing URL is required.")
    if len(value) > 4096:
        raise PortalUrlSafetyError("URL is too long.")

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise PortalUrlSafetyError("URL is malformed.") from exc

    if parts.scheme.lower() not in {"http", "https"}:
        raise PortalUrlSafetyError("Only http and https URLs are allowed.")
    if not parts.hostname:
        raise PortalUrlSafetyError("URL must include a hostname.")
    if parts.username is not None or parts.password is not None:
        raise PortalUrlSafetyError("URLs with embedded credentials are not allowed.")

    hostname = _normalize_hostname(parts.hostname)
    _validate_host_is_public(hostname)

    configured_hosts = normalize_allowed_hosts(allowed_hosts or default_allowed_hosts(hostname))
    if allowed_hosts is not None and not host_is_allowed(hostname, configured_hosts):
        raise PortalUrlSafetyError("URL host is not in this portal's approved host allow-list.")

    try:
        normalized_url = _normalize_url_parts(parts, hostname)
    except ValueError as exc:
        raise PortalUrlSafetyError("URL port is invalid.") from exc

    return ValidatedPortalUrl(
        original_url=value,
        normalized_url=normalized_url,
        hostname=hostname,
        allowed_hosts=configured_hosts,
    )
