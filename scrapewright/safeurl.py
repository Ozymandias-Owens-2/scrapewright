"""Refuse to fetch addresses that belong to us rather than to the internet.

The service fetches whatever URL a caller names, and a key costs nothing, so
without this anyone can use our server as a proxy into places they cannot reach
themselves: the loopback interface, the cloud provider's private network, a
metadata endpoint. ``/v1/extract`` hands back the page it fetched, which turns
that from a port scan into a read.

Three checks, and the third is the one people forget:

* the scheme must be http or https -- ``file://`` reads the container's disk;
* every address the hostname resolves to must be public, not just the first,
  because a name can answer with several;
* redirects must be checked again at each hop, since a public URL is free to
  redirect to ``127.0.0.1`` and a check that only looks at the URL the caller
  typed will wave it through.

A residual risk this does not close: between resolving a name and connecting to
it, DNS can answer differently (rebinding). Closing that properly means pinning
the resolved address into the socket, which is a bigger change than this file.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

import requests

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrl(requests.RequestException):
    """Raised for a URL that points somewhere the service must not go.

    Subclasses ``RequestException`` so that callers already treating a failed
    fetch as "no content" degrade safely rather than crashing.
    """


def _allow_private() -> bool:
    """Self-hosting against an internal site is legitimate; being the default
    is not."""
    return os.environ.get("SCRAPEWRIGHT_ALLOW_PRIVATE", "0").lower() in {
        "1", "true", "yes"}


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # An IPv4 address wearing an IPv6 costume still goes to the same place.
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _as_literal_ip(host: str) -> str | None:
    """Normalise a host written as an address, in any spelling of one.

    ``ipaddress`` rejects the legacy IPv4 forms -- octal ``0177.0.0.1``, the
    32-bit integer ``2130706433``, the short ``127.1`` -- but C's inet_aton
    accepts all of them and so does the resolver underneath a Linux container.
    A check that only understands dotted-decimal is a check with a documented
    bypass printed in every SSRF cheat sheet.
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def resolved_addresses(host: str, port: int | None = None) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def check_url(url: str) -> None:
    """Raise :class:`UnsafeUrl` unless this address belongs to the internet."""
    if _allow_private():
        return

    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl(f"{parts.scheme or 'that'} is not a scheme this service "
                        f"will fetch; use http or https")

    host = parts.hostname
    if not host:
        raise UnsafeUrl("that URL names no host")

    literal = _as_literal_ip(host)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = resolved_addresses(host, parts.port)
        if not addresses:
            # Unresolvable is the fetcher's problem to report, not a refusal.
            return

    for address in addresses:
        if not _is_public(address):
            raise UnsafeUrl(
                f"{host} resolves to {address}, which is not a public address. "
                f"This service will not fetch private or loopback addresses.")
