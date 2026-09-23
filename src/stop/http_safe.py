"""Outbound HTTP for turn streams — allowlist, no credentialed redirects.

Customer installs must not follow a ``Location`` to an attacker host while
still carrying ``Authorization: Bearer …``. Loopback stays http-friendly;
everything else needs https unless ``STOP_HTTP_INSECURE=1``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse


class HttpPolicyError(ValueError):
    """URL rejected by stop's outbound policy."""


def _hostname_is_loopback(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def validate_outbound_url(url: str, *, allow_insecure: bool | None = None) -> str:
    """Return a normalized URL or raise ``HttpPolicyError``."""
    raw = (url or "").strip()
    if not raw:
        raise HttpPolicyError("empty URL")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    if scheme not in ("http", "https"):
        raise HttpPolicyError(f"unsupported scheme: {scheme or '(none)'}")
    if not host:
        raise HttpPolicyError("URL missing host")
    insecure = (
        allow_insecure
        if allow_insecure is not None
        else os.environ.get("STOP_HTTP_INSECURE", "0") == "1"
    )
    loopback = _hostname_is_loopback(host)
    if scheme == "http" and not loopback and not insecure:
        raise HttpPolicyError(
            f"non-loopback http blocked for {host} (set STOP_HTTP_INSECURE=1 to opt in)"
        )
    return raw


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects but never re-send Authorization / Cookie."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )
        if new is None:
            return None
        for header in ("Authorization", "Cookie", "Proxy-Authorization"):
            if header in new.headers:
                del new.headers[header]
            # Request.headers is case-insensitive mapping in some Pythons.
            try:
                new.remove_header(header)
            except Exception:
                pass
        # Re-validate the redirect target.
        validate_outbound_url(new.full_url)
        return new


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoCredentialRedirect)


def urlopen(
    req: urllib.request.Request,
    *,
    timeout: float,
    allow_insecure: bool | None = None,
):
    """``urlopen`` with allowlist + redirect stripping."""
    validate_outbound_url(req.full_url, allow_insecure=allow_insecure)
    opener = build_opener()
    return opener.open(req, timeout=timeout)


def http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict | None = None,
    timeout: float = 10.0,
    allow_insecure: bool | None = None,
) -> Any:
    validate_outbound_url(url, allow_insecure=allow_insecure)
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout, allow_insecure=allow_insecure) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    return json.loads(raw)


def resolve_allowed(host: str) -> bool:
    """True when DNS resolves only to loopback (optional strict check)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_loopback:
                return False
        except (ValueError, IndexError):
            return False
    return bool(infos)
