"""Refuse to fetch from servers that are not on the public internet.

A handle is typed by the person under review, and on some platforms it names
the server to ask. Without this check a handle such as user@127.0.0.1 would
make this machine send requests to itself or to its local network.

The check resolves the name once, before the request. A server that changes
its address between the check and the request is not caught.
"""

import ipaddress
import re
import socket
from collections.abc import Callable

Resolver = Callable[[str], list[str]]

# A dotted name ending in letters: no bare words, no addresses, no ports.
_PUBLIC_NAME = re.compile(
    r"(?=.{4,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
)


def resolve(host: str) -> list[str]:
    """Every address a server name resolves to."""
    found = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in found})


def is_public_name(host: str) -> bool:
    """True for a dotted server name such as mastodon.social."""
    return bool(_PUBLIC_NAME.fullmatch(host.lower()))


def refusal(host: str, resolver: Resolver = resolve) -> str | None:
    """Why this server must not be fetched from, or None when it may be."""
    name = host.strip().lower()
    if not is_public_name(name):
        return "that is not a public server name"
    try:
        addresses = resolver(name)
    except OSError:
        return "the server name does not resolve"
    if not addresses:
        return "the server name does not resolve"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return "the server's address could not be read"
        mapped = getattr(ip, "ipv4_mapped", None)
        if not (mapped or ip).is_global:
            return "the server is not on the public internet"
    return None
