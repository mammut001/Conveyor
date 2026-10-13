"""Fail-closed, address-family-aware policy for the opt-in loopback egress proxy."""
from __future__ import annotations

import ipaddress
import os
import socket


_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def private_cidrs(settings=None):
    raw = getattr(settings, "egress_allow_private_cidrs", None)
    if raw is None:
        raw = os.getenv("CONVEYOR_EGRESS_ALLOW_PRIVATE_CIDRS", "")
    # Invalid CIDRs prevent startup; ignoring misconfiguration risks broadening.
    return tuple(ipaddress.ip_network(part.strip(), strict=False)
                 for part in str(raw).split(",") if part.strip())


def embedded_ipv4(addr):
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in _NAT64_WKP or addr in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(addr) & 0xffffffff)
    return None


def forbidden(addr):
    # No allowlist or CIDR entry can override these, including translated IPs.
    if (addr.is_loopback or addr.is_link_local or addr.is_unspecified or
            addr.is_multicast):
        return True
    embedded = embedded_ipv4(addr)
    return embedded is not None and forbidden(embedded)


def permitted_ip(addr, cidrs):
    if forbidden(addr):
        return False, "forbidden_address"
    if addr.is_global:
        return True, "allowlisted"
    if any(addr in net for net in cidrs):
        return True, "private_cidr_allowlisted"
    return False, "private_ip"


def destination_check(host, port, rules, *, canonical, match,
                      resolver=socket.getaddrinfo, cidrs=(), protected_ports=()):
    """No connection until every DNS answer passes (including exact hosts)."""
    try:
        host = canonical(host)
        port = int(port)
        if not 1 <= port <= 65535:
            return None, "invalid_port"
        # Conservative port-wide refusal also covers every local interface,
        # even if the public IP of this server resolves via a DNS alias.
        if port in protected_ports:
            return None, "protected_port"
        _, allowed = match(host, rules)
        if not allowed:
            return None, "not_allowlisted"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            ok, reason = permitted_ip(literal, cidrs)
            return (str(literal), reason) if ok else (None, reason)
        answers = {ipaddress.ip_address(row[4][0]) for row in
                   resolver(host, port, type=socket.SOCK_STREAM)}
        if not answers:
            return None, "dns_empty"
        for ip in answers:
            ok, reason = permitted_ip(ip, cidrs)
            if not ok:
                return None, "forbidden_dns" if reason == "forbidden_address" else "private_dns"
        ip = sorted(answers, key=lambda addr: (addr.version, int(addr)))[0]
        return str(ip), permitted_ip(ip, cidrs)[1]
    except (OSError, ValueError, UnicodeError, IndexError):
        return None, "dns_or_host_error"
