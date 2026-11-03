"""
core/ssrf_guard.py — OADSON V3 shared SSRF protection.

Extracted 2026-08-14 from skills/web_fetch/skill.py, which had the only
real implementation of this check in the whole codebase. Five other
skills fetch an arbitrary caller-supplied URL exactly the same way
(google_drive's read_url action, api_tester, docs_fetcher,
header_analyzer, html_preview) and had ZERO protection — none of them
even had the old string-prefix version web_fetch used to have, let
alone this real one. Any of them could be pointed at
http://169.254.169.254/... (cloud metadata) or http://localhost:PORT/...
and would fetch and return it without complaint.

One shared implementation now, so a future fix (or the DNS-rebinding
residual gap noted below) only needs to happen in one place instead of
six.

Usage — before making any request to a caller-supplied URL:

    from core.ssrf_guard import is_safe_url
    safe, reason = await is_safe_url(url)
    if not safe:
        return {"status": "error", "reason": reason}
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def _is_blocked_ip(ip_str: str) -> bool:
    """True if this address is private/loopback/link-local/reserved —
    covers full IPv4 (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
    192.168.0.0/16, 169.254.0.0/16) and IPv6 (::1, fc00::/7, fe80::/10)
    ranges via the stdlib ipaddress module rather than hand-rolled
    string prefixes, which would only match a handful of literal
    strings and miss most of each range (e.g. only "127.0.0.1" exactly,
    not the rest of 127.0.0.0/8; no IPv6 coverage at all)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # can't parse it — fail closed, don't fetch it
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


async def is_safe_url(url: str) -> tuple[bool, str]:
    """
    Check if a URL is safe to fetch. Returns (is_safe, reason_if_not).

    Resolves the hostname and checks EVERY returned address (IPv4 and
    IPv6) against the real private/loopback/link-local/reserved ranges
    — a hostname like "internal.example.com" that *resolves* to
    127.0.0.1 or 10.x.x.x is caught here, not just literal IP URLs.

    HONEST RESIDUAL LIMITATION: this checks DNS once, up front, before
    the actual request. A sufficiently fast DNS rebinding attack (very
    low TTL, re-resolving between this check and the request library's
    own connection) could theoretically still slip an internal IP past
    this check. Fully closing that gap requires resolving once and
    connecting to that pinned IP directly (with the original hostname
    only used for the Host header / TLS SNI), which is meaningfully
    more complex and not implemented here. This closes the actual bug
    that's exploitable with zero DNS trickery at all; the rebinding
    race is a narrower, harder-to-exploit residual risk worth knowing
    about.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "Only http/https URLs are allowed"

        hostname = parsed.hostname
        if not hostname:
            return False, "URL has no hostname"

        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        except socket.gaierror as e:
            return False, f"Could not resolve hostname: {e}"

        if not infos:
            return False, "Hostname resolved to no addresses"

        for _family, _type, _proto, _canon, sockaddr in infos:
            ip_str = sockaddr[0]
            if _is_blocked_ip(ip_str):
                return False, "URL resolves to a private/internal address — blocked for security"

        return True, ""
    except Exception as e:
        return False, f"URL validation failed: {e}"
