"""Canonical, port-scoped host rules for the opt-in egress proxy.

A bare name permits only TLS port 443. Port 80 (or any other) must be
explicit: example.com:80 or *.example.com:8443. Reject ambiguous DNS labels.
"""
from __future__ import annotations

import ipaddress
import re


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def canonical_host(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("invalid_host")
    if any(ch in host for ch in "/\\@% \r\n\t") or host.startswith("."):
        raise ValueError("invalid_host")
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]") and host.count("[") == 1
                and host.count("]") == 1):
            raise ValueError("invalid_host")
        try:
            return str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:
            raise ValueError("invalid_host") from None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    # DNS permits one terminal root dot, never two or interior empty labels.
    if host.endswith("..") or ".." in host:
        raise ValueError("invalid_host")
    name = host[:-1] if host.endswith(".") else host
    if not name or ":" in name or "[" in name or "]" in name:
        raise ValueError("invalid_host")
    try:
        name = name.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("invalid_host") from None
    if len(name) > 253 or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        raise ValueError("invalid_host")
    return name


def _rule(value):
    value = value.strip()
    wildcard = value.startswith("*.")
    if wildcard:
        value = value[2:]
    # Brackets disambiguate IPv6 literal + optional numeric port.
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise ValueError("invalid_rule")
        host, rest = value[:close + 1], value[close + 1:]
        if rest and not rest.startswith(":"):
            raise ValueError("invalid_rule")
        port_text = rest[1:] if rest else None
    elif value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
    else:
        host, port_text = value, None
    try:
        port = 443 if port_text is None else int(port_text)
        if port_text is not None and (
            not port_text.isascii() or not port_text.isdecimal()
            or port_text != str(port)
        ):
            # Do not silently canonicalize ambiguous ports such as :0443.
            raise ValueError("invalid_rule")
        if not 1 <= port <= 65535:
            raise ValueError("invalid_rule")
        name = canonical_host(host)
    except (TypeError, ValueError):
        raise ValueError("invalid_rule") from None
    if wildcard and (":" in name or name.replace(".", "").isdigit()):
        raise ValueError("invalid_rule")
    return name, port, wildcard


def normalize_rules(raw):
    rules = set()
    for item in str(raw).split(","):
        if not item.strip():
            continue
        name, port, wildcard = _rule(item)
        authority = f"[{name}]" if ":" in name else name
        rules.add(("*." if wildcard else "") + authority +
                  (f":{port}" if port != 443 else ""))
    return tuple(sorted(rules))


def match(host, port, rules):
    """Match a *canonical* host and requested port against exact/wildcard rules."""
    exact = False
    wildcard = False
    for item in rules:
        name, allowed_port, is_wildcard = _rule(item)
        if port != allowed_port:
            continue
        if is_wildcard:
            wildcard |= host.endswith("." + name) and host != name
        else:
            exact |= host == name
    return exact, exact or wildcard
