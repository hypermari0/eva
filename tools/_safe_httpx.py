"""SSRF-safe httpx wrapper for the dynamic-skill sandbox.

Every request resolves its hostname and rejects any target that lands on a
private, loopback, link-local, multicast, reserved, or unspecified address —
blocking cloud metadata endpoints (169.254.169.254, metadata.google.internal),
internal LAN (10/8, 192.168/16, 172.16/12), and IPv6 equivalents. Only http
and https schemes are allowed; AsyncClient is disabled (sandbox is sync-only).

NOTE: there is a small TOCTOU window between DNS resolution here and the one
httpx performs when it opens the socket. For a single-user personal bot the
approval gate makes this acceptable; harder isolation requires a network
namespace / egress proxy.
"""

import ipaddress
import socket
import types
from urllib.parse import urlparse

import httpx as _httpx

_BLOCKED_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata",
    "metadata.goog",
    "metadata.aws",
})


def _check_ip(ip: ipaddress._BaseAddress, host: str) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(
            f"Blocked target: {host} resolves to {ip} "
            "(private / loopback / link-local / reserved range)"
        )


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}': only http/https allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")
    if host.lower() in _BLOCKED_HOSTS:
        raise ValueError(f"Blocked metadata host: {host}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        _check_ip(literal, host)
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        _check_ip(ip, host)


class _SafeClient(_httpx.Client):
    def send(self, request, *args, **kwargs):
        _validate_url(str(request.url))
        return super().send(request, *args, **kwargs)


def _wrap_method(name: str):
    fn = getattr(_httpx, name)

    def wrapped(url, *args, **kwargs):
        _validate_url(str(url))
        return fn(url, *args, **kwargs)

    wrapped.__name__ = name
    wrapped.__doc__ = f"SSRF-guarded wrapper around httpx.{name}."
    return wrapped


def _no_async(*_args, **_kwargs):
    raise RuntimeError(
        "httpx.AsyncClient is not available in dynamic skills. Use httpx.Client() (sync)."
    )


def build_safe_httpx() -> types.ModuleType:
    """Return a module-like object that mimics httpx but validates every URL."""
    safe = types.ModuleType("httpx")
    safe.Client = _SafeClient
    safe.AsyncClient = _no_async
    for method in ("get", "post", "put", "patch", "delete", "head", "options", "request"):
        setattr(safe, method, _wrap_method(method))
    # Re-export common exception types so skills can catch them
    for attr in (
        "HTTPError", "HTTPStatusError", "RequestError",
        "TimeoutException", "ConnectError", "ReadError",
        "Response", "Timeout",
    ):
        if hasattr(_httpx, attr):
            setattr(safe, attr, getattr(_httpx, attr))
    return safe
