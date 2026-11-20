"""Link validation (stdlib only).

Accepts only http(s) URLs with a DNS hostname that resolves exclusively to
public addresses. IP literals (in any notation) are rejected outright.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
_NUMERIC_LABEL = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$", re.IGNORECASE)
_HOST_CHARS = re.compile(r"^[a-z0-9.\-_]+$", re.IGNORECASE)


class UrlError(ValueError):
    """Raised with a short, client-safe message."""


def _looks_like_ip(host: str) -> bool:
    if ":" in host or "%" in host or "[" in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # WHATWG rule: a host whose last label is numeric (decimal, octal or
    # 0x-hex) is parsed as IPv4 by browsers and resolvers (2130706433, 0x7f.1, ...).
    last = host.rstrip(".").rsplit(".", 1)[-1]
    return bool(_NUMERIC_LABEL.match(last))


def is_public_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return ip.is_global


def check_url(url: str) -> str:
    """Return the stripped URL if it is acceptable, else raise UrlError."""
    if not isinstance(url, str):
        raise UrlError("link must be text")
    url = url.strip()
    if not url:
        raise UrlError("link is empty")
    if len(url) > MAX_URL_LENGTH:
        raise UrlError("link is too long")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise UrlError("link contains invalid characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlError("link is not valid")
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlError("only http and https links are supported")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UrlError("links with a username or password are not allowed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UrlError("link has no host name")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlError("link host name is not valid")
    if _looks_like_ip(host):
        raise UrlError("links to IP addresses are not allowed")
    if not _HOST_CHARS.match(host):
        raise UrlError("link host name is not valid")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UrlError("links to this server's network are not allowed")
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise UrlError("could not resolve the link's host name")
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise UrlError("could not resolve the link's host name")
    if not all(is_public_address(a) for a in addrs):
        raise UrlError("links to private or local network addresses are not allowed")
    return url
