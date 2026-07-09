"""Normalization and policy checks for outbound service URLs."""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from .config import settings


def _parse_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _format_netloc(host: str, port: int | None) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    formatted_host = host
    if ip and ip.version == 6:
        formatted_host = f"[{host}]"

    if port is None:
        return formatted_host
    return f"{formatted_host}:{port}"


def _host_matches_allowlist(host: str, allowed_hosts: list[str]) -> bool:
    normalized_host = _normalize_host(host)
    for candidate in allowed_hosts:
        normalized_candidate = _normalize_host(candidate)
        if normalized_host == normalized_candidate:
            return True
        if normalized_host.endswith(f".{normalized_candidate}"):
            return True
    return False


@lru_cache(maxsize=256)
def _resolve_host_ips(host: str) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass

    addresses: set[str] = set()
    for result in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
        address = result[4][0]
        addresses.add(address)
    return tuple(sorted(addresses))


def _validate_allowlists(host: str) -> None:
    allowed_hosts = _parse_csv(settings.OUTBOUND_URL_ALLOWED_HOSTS)
    allowed_cidrs = [ipaddress.ip_network(entry, strict=False) for entry in _parse_csv(settings.OUTBOUND_URL_ALLOWED_CIDRS)]

    if not allowed_hosts and not allowed_cidrs:
        return

    if allowed_hosts and _host_matches_allowlist(host, allowed_hosts):
        return

    if allowed_cidrs:
        resolved_ips = _resolve_host_ips(host)
        if resolved_ips:
            ip_objects = [ipaddress.ip_address(address) for address in resolved_ips]
            if all(any(ip in network for network in allowed_cidrs) for ip in ip_objects):
                return

    raise ValueError("URL host is not allowed by the outbound access policy")


def normalize_service_url(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None

    value = raw_value.strip()
    if not value:
        return None

    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if parts.username or parts.password:
        raise ValueError("Credentials must not be embedded in the URL")
    if parts.query or parts.fragment:
        raise ValueError("URL query parameters and fragments are not allowed")
    if parts.path not in ("", "/"):
        raise ValueError("URL path must be empty")

    host = parts.hostname
    if not host:
        raise ValueError("URL host is required")

    _validate_allowlists(host)

    port = parts.port
    netloc = _format_netloc(host.lower(), port)
    return urlunsplit((scheme, netloc, "", "", ""))
