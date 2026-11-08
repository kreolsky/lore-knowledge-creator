"""Outbound egress guard — the backend dials only public, operator-allowed addresses.

# SYSTEM: egress-guard — resolve-and-refuse for backend-originated HTTP whose
# destination is user input. Shared by the http_request tool (SYSTEM:
# user-secrets) and, later, external MCP server URLs; reuse is by calling this
# module, never by copying the address checks.

Guards backend-originated HTTP whose destination is user input (the
http_request tool against a user's stored secret; external MCP server URLs
later). Resolving-and-refusing here is not enough by itself: the caller must
connect to one of the RESOLVED addresses (original name as SNI/Host), which is
what closes DNS rebinding — a name that re-resolves to a private address
between the check and the connect changes nothing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import config


class EgressRefused(ValueError):
    """The host resolves to an address the backend refuses to dial."""


def _allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """OUTBOUND_ALLOW_CIDRS parsed per call (live for tests and env flips)."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    raw = (getattr(config, "OUTBOUND_ALLOW_CIDRS", "") or "").strip()
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _refused(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Refuse private/loopback/link-local/CGNAT/multicast/reserved/unspecified.

    WHY `is_global`, not `is_private`: CGNAT 100.64/10 is neither private nor
    global, so `is_private` lets Tailscale and cloud-internal addresses through.
    The explicit extra checks cover what `is_global` leaves True (multicast).
    """
    return (
        not ip.is_global
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def resolve_outbound_host(host: str) -> list[str]:
    """Resolve `host`; return its IP literals, or raise EgressRefused.

    Every resolved address must be public (or inside OUTBOUND_ALLOW_CIDRS) —
    one bad address in the answer refuses the call, it is not filtered around:
    a DNS answer the caller partially trusts is exactly the rebinding shape
    this guard exists against.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise EgressRefused(f"cannot resolve host: {host}") from exc
    allowed = _allowed_networks()
    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _refused(ip) and not any(ip in net for net in allowed):
            raise EgressRefused(f"{host} resolves to a refused address: {ip}")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise EgressRefused(f"{host} resolved to no addresses")
    return ips
