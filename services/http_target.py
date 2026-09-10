"""Validated and DNS-pinned options for configured HTTP integrations."""

from __future__ import annotations

from ipaddress import ip_address
import socket
from urllib.parse import urlsplit

from curl_cffi import CurlOpt


class HttpTargetError(ValueError):
    """Raised when a configured HTTP target cannot be used safely."""


def resolve_http_target(url: object) -> tuple[str, tuple[str, ...]]:
    """Validate an HTTP(S) URL and resolve its host for connection pinning."""

    text = str(url or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise HttpTargetError("configured HTTP target URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HttpTargetError("configured HTTP target must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise HttpTargetError("configured HTTP target must not include credentials")
    host = str(parsed.hostname or "").strip()
    if not host:
        raise HttpTargetError("configured HTTP target must include a host")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise HttpTargetError("configured HTTP target port is invalid") from exc

    try:
        ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HttpTargetError("configured HTTP target host could not be resolved") from exc
        addresses = {
            str(info[4][0])
            for info in infos
            if len(info) >= 5 and info[4] and str(info[4][0]).strip()
        }
    else:
        addresses = {host}
    if not addresses:
        raise HttpTargetError("configured HTTP target host could not be resolved")
    return text, tuple(sorted(addresses))


def build_http_target_request_options(
    url: object,
    *,
    allow_redirects: bool = False,
) -> dict[str, object]:
    """Return request options with the validated host pinned to its addresses."""

    text, addresses = resolve_http_target(url)
    parsed = urlsplit(text)
    host = str(parsed.hostname or "").strip()
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        ip_address(host)
    except ValueError:
        resolve = [
            f"{host}:{port}:{f'[{address}]' if ':' in address else address}"
            for address in addresses
        ]
    else:
        resolve = []
    return {
        "allow_redirects": bool(allow_redirects),
        "curl_options": {CurlOpt.RESOLVE: resolve} if resolve else {},
    }
