"""Shared SSRF egress filtering (DNS-pinned) and stdio MCP command allowlist guards."""

from __future__ import annotations

import asyncio
import os
import re
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit, urlunsplit

_SAFE_COMMAND_RE = re.compile(r"^[A-Za-z0-9._:/\\-]{1,256}\Z")
_DEFAULT_ALLOWED_PORTS = frozenset({80, 443})


@dataclass(frozen=True, slots=True)
class PinnedHttpTarget:
    """Validated public HTTP(S) target pinned to a single verified global IP."""

    url: str
    connect_url: str
    scheme: str
    hostname: str
    port: int
    host_header: str
    pinned_ip: str


def is_global_public_ip(raw_ip: str) -> bool:
    """Return True iff raw_ip parses to a globally routable public IP address."""
    try:
        addr = ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, (IPv4Address, IPv6Address)):
        return bool(
            addr.is_global
            and not addr.is_private
            and not addr.is_loopback
            and not addr.is_link_local
            and not addr.is_multicast
            and not addr.is_reserved
            and not addr.is_unspecified
        )
    return False


def _format_host_header(hostname: str, port: int, scheme: str) -> str:
    formatted_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    default_port = 443 if scheme == "https" else 80
    return formatted_host if port == default_port else f"{formatted_host}:{port}"


def _format_ip_netloc(pinned_ip: str, port: int, scheme: str) -> str:
    ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    default_port = 443 if scheme == "https" else 80
    return ip_host if port == default_port else f"{ip_host}:{port}"


async def resolve_pinned_public_http_target(
    raw_url: object,
    *,
    require_https: bool = False,
    allowed_ports: frozenset[int] | None = None,
) -> PinnedHttpTarget:
    """Validate URL syntax, scheme, ports, and DNS records; return a DNS-pinned target."""
    if not isinstance(raw_url, str) or not raw_url.strip() or any(ch.isspace() for ch in raw_url):
        raise ValueError("URL must be a valid public HTTP(S) address without whitespace")
    try:
        parsed = urlsplit(raw_url)
        port_val = parsed.port
    except ValueError as exc:
        raise ValueError("Malformed URL") from exc

    scheme = parsed.scheme.lower()
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if scheme not in allowed_schemes or not parsed.hostname:
        raise ValueError(
            "Only public HTTPS URLs are permitted" if require_https else "Only public HTTP(S) URLs are permitted"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not permitted")

    port = port_val or (443 if scheme == "https" else 80)
    if allowed_ports is not None and port not in allowed_ports:
        raise ValueError(f"Port {port} is not permitted for outbound requests")

    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("Loopback and local hostnames are blocked")

    resolved_ips: list[str] = []
    try:
        literal_ip = ip_address(hostname)
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError("Unable to resolve outbound host") from exc
        for record in records:
            ip_str = str(record[4][0]).split("%", 1)[0]
            if ip_str not in resolved_ips:
                resolved_ips.append(ip_str)
    else:
        resolved_ips = [str(literal_ip)]

    if not resolved_ips or any(not is_global_public_ip(ip_str) for ip_str in resolved_ips):
        raise ValueError("Outbound requests to loopback, private, link-local, or reserved IPs are blocked")

    pinned_ip = resolved_ips[0]
    normalized_path = parsed.path or "/"
    normalized_url = urlunsplit((scheme, parsed.netloc, normalized_path, parsed.query, ""))
    connect_netloc = _format_ip_netloc(pinned_ip, port, scheme)
    connect_url = urlunsplit((scheme, connect_netloc, normalized_path, parsed.query, ""))
    return PinnedHttpTarget(
        url=normalized_url,
        connect_url=connect_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        host_header=_format_host_header(hostname, port, scheme),
        pinned_ip=pinned_ip,
    )


def validate_stdio_mcp_command(
    command: object,
    *,
    allow_stdio: bool,
    allowed_commands: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> str:
    """Enforce opt-in operator config + executable allowlist for stdio MCP servers."""
    if not allow_stdio:
        raise PermissionError("stdio MCP servers are disabled (set SAG_ALLOW_STDIO_MCP=true with an allowlist)")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("MCP stdio command must be a non-empty executable name")
    cleaned = command.strip()
    if not _SAFE_COMMAND_RE.fullmatch(cleaned) or ".." in cleaned:
        raise ValueError("MCP stdio command contains disallowed characters")
    allowlist = {item.strip() for item in allowed_commands if isinstance(item, str) and item.strip()}
    if not allowlist:
        raise PermissionError("No stdio MCP executables are allowlisted in SAG_MCP_STDIO_ALLOWED_COMMANDS")
    basename = os.path.basename(cleaned)
    if cleaned not in allowlist and basename not in allowlist:
        raise PermissionError(f"MCP stdio command '{basename}' is not in the operator allowlist")
    return cleaned
