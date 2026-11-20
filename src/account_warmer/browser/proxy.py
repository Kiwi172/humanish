"""Proxy URL validation → Playwright proxy dict (SPEC §6.3, account row / .env fallback).

Supported schemes: ``http://``, ``https://``, ``socks5://`` (with optional user:pass auth).
socks5 is preferred — DNS also exits through the proxy, closing the WebRTC/DNS leak
(SPEC R3). Datacenter IPs are the #1 shared tell across accounts (R1) — operator's job.
"""
from __future__ import annotations

from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https", "socks5")


class ProxyError(ValueError):
    """Raised when a proxy URL is malformed or uses an unsupported scheme."""


def parse_proxy(url: str | None) -> dict[str, str] | None:
    """Parse a proxy URL into the Playwright ``proxy`` mapping.

    Returns ``None`` for empty input (== no proxy). Raises :class:`ProxyError` on bad
    scheme/missing host or port. Username/password are included only when present.
    """
    if url is None:
        return None
    text = str(url).strip()
    if not text:
        return None

    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ProxyError(
            f"proxy scheme {scheme!r} not supported; expected one of {ALLOWED_SCHEMES}"
        )
    host, port = parts.hostname, parts.port
    if not host or not port:
        raise ProxyError(f"proxy URL must include host and explicit port: {text!r}")

    result: dict[str, str] = {"server": f"{host}:{port}"}
    if parts.username:
        result["username"] = parts.username
    if parts.password is not None:
        result["password"] = parts.password
    return result


def redact_proxy(url: str | None) -> str | None:
    """Mask the password for safe logging (e.g. sessions.proxy_used, event detail)."""
    if url is None or not str(url).strip():
        return None
    text = str(url).strip()
    parts = urlsplit(text)
    if not parts.scheme or not parts.hostname:
        return "***"  # unparseable — don't leak anything
    user = parts.username or ""
    cred = f"{user}:***@" if (parts.username and parts.password is not None) else (f"{user}@" if parts.username else "")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{cred}{parts.hostname}{port}"
