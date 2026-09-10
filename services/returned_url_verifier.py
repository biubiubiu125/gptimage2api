from __future__ import annotations

from ipaddress import ip_address
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from curl_cffi import CurlOpt, requests as curl_requests

from services.browser_fingerprint import (
    chrome146_headers,
    chrome146_session_kwargs,
    chrome146_signed_asset_headers,
)
from services.image_url import normalize_url_origin


class ReturnedUrlVerificationError(RuntimeError):
    pass


class _ReturnedUrlResponseTooLarge(RuntimeError):
    pass


class _PinnedResponse:
    def __init__(self, response, session, payload: bytes | None = None) -> None:  # noqa: ANN001
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            if callable(getcode):
                try:
                    status = getcode()
                except Exception:
                    status = 0
        self.status = int(status or 0)
        self.headers = getattr(response, "headers", {}) or {}
        if payload is None:
            body = getattr(response, "content", None)
            if body is None:
                read = getattr(response, "read", None)
                if callable(read):
                    body = read()
                else:
                    body = b""
            self._payload = bytes(body)
        else:
            self._payload = bytes(payload)
        self._session = session

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def content(self) -> bytes:
        return self._payload

    @property
    def text(self) -> str:
        return self._payload.decode("utf-8", errors="replace")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        self._session.close()
        return False

    def close(self) -> None:
        self._session.close()

    def getcode(self) -> int:
        return self.status

    def iter_content(self, chunk_size: int = 1):  # noqa: ANN201
        step = max(1, int(chunk_size or 1))
        for start in range(0, len(self._payload), step):
            yield self._payload[start : start + step]

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]


def urlopen(request: Request, timeout: float, *, proxy: str = "", verify: bool = True):  # noqa: ANN201
    parsed = urlsplit(request.full_url)
    host = str(parsed.hostname or "").strip()
    max_bytes = max(0, int(getattr(request, "_gptimage2api_max_bytes", 0) or 0))
    verified_addresses = tuple(
        str(value or "").strip()
        for value in getattr(request, "_gptimage2api_verified_addresses", ())
        if str(value or "").strip()
    )
    addresses = verified_addresses
    if not addresses:
        try:
            ip_address(host)
        except ValueError:
            addresses = _validate_public_host(parsed)
        else:
            addresses = (host,)
    if host and addresses:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        last_error: Exception | None = None
        for address in addresses:
            session = None
            payload = bytearray()
            too_large = False

            def receive(chunk: bytes) -> int:
                nonlocal too_large
                if max_bytes > 0 and len(payload) + len(chunk) > max_bytes:
                    too_large = True
                    raise _ReturnedUrlResponseTooLarge()
                payload.extend(chunk)
                return len(chunk)

            try:
                resolve = (
                    [f"{host}:{port}:{_curl_resolve_address(address)}"]
                    if host != address
                    else []
                )
                session_kwargs = chrome146_session_kwargs({
                    "allow_redirects": False,
                    "trust_env": False,
                    "verify": verify,
                    "curl_options": {CurlOpt.RESOLVE: resolve} if resolve else {},
                })
                if proxy:
                    session_kwargs["proxy"] = proxy
                session = curl_requests.Session(**session_kwargs)
                request_kwargs = {
                    "headers": chrome146_headers(dict(request.header_items())),
                    "timeout": timeout,
                    "allow_redirects": False,
                }
                if max_bytes > 0:
                    request_kwargs["content_callback"] = receive
                response = session.request(
                    request.get_method(),
                    request.full_url,
                    **request_kwargs,
                )
                bounded_payload = bytes(payload) if max_bytes > 0 else None
                return _PinnedResponse(response, session, bounded_payload)
            except Exception as exc:
                last_error = _ReturnedUrlResponseTooLarge() if too_large else exc
                if session is not None:
                    session.close()
        if isinstance(last_error, _ReturnedUrlResponseTooLarge):
            raise last_error
        raise URLError(str(last_error or "no verified address could be reached")) from last_error
    raise URLError("returned image URL has no verified public address")


def _looks_like_image(payload: bytes) -> bool:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if payload.startswith(b"\xff\xd8\xff"):
        return True
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return True
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return True
    return False


def _curl_resolve_address(address: str) -> str:
    value = str(address or "").strip()
    if ":" in value and not value.startswith("["):
        return f"[{value}]"
    return value


def _reject_private_or_local_address(address: str) -> None:
    try:
        parsed_ip = ip_address(address)
    except ValueError:
        return
    if not parsed_ip.is_global:
        raise ReturnedUrlVerificationError("returned image URL resolves to a private or local address")


def _validate_public_host(parsed, *, resolve_dns: bool = True) -> tuple[str, ...]:  # noqa: ANN001
    if parsed.username is not None or parsed.password is not None:
        raise ReturnedUrlVerificationError("returned image URL must not include credentials")
    host = str(parsed.hostname or "").strip()
    if not host:
        raise ReturnedUrlVerificationError("returned image URL must include a host")
    lowered = host.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise ReturnedUrlVerificationError("returned image URL resolves to a private or local address")
    try:
        parsed.port
    except ValueError as exc:
        raise ReturnedUrlVerificationError("returned image URL port is invalid") from exc
    try:
        ip_address(host)
    except ValueError:
        if not resolve_dns:
            return ()
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ReturnedUrlVerificationError("returned image URL port is invalid") from exc
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ReturnedUrlVerificationError("returned image URL host could not be resolved") from exc
        addresses = {
            str(info[4][0])
            for info in infos
            if len(info) >= 5 and info[4]
        }
        if not addresses:
            raise ReturnedUrlVerificationError("returned image URL host could not be resolved")
        for address in addresses:
            _reject_private_or_local_address(address)
        return tuple(sorted(addresses))
    _reject_private_or_local_address(host)
    return (host,)


def _build_verified_returned_image_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = 0,
    allowed_base_url: str = "",
) -> Request:
    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReturnedUrlVerificationError("returned image URL must be an http or https URL")
    _validate_allowed_base_url(text, allowed_base_url)
    verified_addresses = _validate_public_host(parsed)
    request = Request(text, headers=dict(headers or {}), method="GET")
    setattr(request, "_gptimage2api_verified_addresses", verified_addresses)
    setattr(request, "_gptimage2api_max_bytes", max(0, int(max_bytes or 0)))
    return request


def validate_public_image_base_url(value: object, *, resolve_host: bool = False) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ReturnedUrlVerificationError(
            "worker image base URL must be an http or https URL without query or fragment"
        )
    _validate_public_host(parsed, resolve_dns=resolve_host)
    path = parsed.path.rstrip("/")
    if path not in {"", "/images"}:
        raise ReturnedUrlVerificationError("worker image base URL path must be empty or /images")
    return text


def _validate_allowed_base_url(url: str, allowed_base_url: str) -> None:
    base = str(allowed_base_url or "").strip().rstrip("/")
    if not base:
        return
    parsed_url = urlsplit(url)
    parsed_base = urlsplit(base)
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise ReturnedUrlVerificationError("worker image base URL must be an http or https URL without query or fragment")
    url_origin = normalize_url_origin(url)
    base_origin = normalize_url_origin(base)
    if url_origin is None or base_origin is None or url_origin != base_origin:
        raise ReturnedUrlVerificationError("returned image URL is outside the worker image base URL")
    base_path = parsed_base.path.rstrip("/")
    if not base_path:
        return
    target_path = parsed_url.path.rstrip("/")
    if target_path == base_path or target_path.startswith(f"{base_path}/"):
        return
    raise ReturnedUrlVerificationError("returned image URL is outside the worker image base URL")


def validate_returned_image_url_target(
    url: str,
    *,
    allowed_base_url: str = "",
) -> str:
    text = str(url or "").strip()
    _build_verified_returned_image_request(text, headers={}, allowed_base_url=allowed_base_url)
    return text


def open_verified_returned_image_url(
    url: str,
    *,
    timeout_seconds: float = 5.0,
    max_bytes: int = 65536,
    headers: dict[str, str] | None = None,
    allowed_base_url: str = "",
    proxy: str = "",
    verify: bool = True,
):  # noqa: ANN201
    request_headers = chrome146_signed_asset_headers(
        {
            str(key): value
            for key, value in (headers or {}).items()
            if value is not None
        },
    )
    request_headers.setdefault("Accept", "image/*,*/*;q=0.8")
    if max_bytes > 0:
        request_headers.setdefault("Range", f"bytes=0-{max(1, int(max_bytes or 0)) - 1}")
    request = _build_verified_returned_image_request(
        url,
        headers=request_headers,
        max_bytes=max_bytes,
        allowed_base_url=allowed_base_url,
    )
    timeout = max(0.5, float(timeout_seconds or 5.0))
    if not proxy and verify:
        return urlopen(request, timeout=timeout)
    return urlopen(request, timeout=timeout, proxy=proxy, verify=verify)


def verify_returned_image_url(
    url: str,
    *,
    timeout_seconds: float = 5.0,
    attempts: int = 3,
    max_bytes: int = 65536,
    allowed_base_url: str = "",
    proxy: str = "",
    verify: bool = True,
) -> None:
    last_error: Exception | None = None
    total_attempts = max(1, int(attempts or 1))
    for index in range(total_attempts):
        response = None
        try:
            byte_limit = max(512, int(max_bytes or 65536))
            response = open_verified_returned_image_url(
                url,
                timeout_seconds=timeout_seconds,
                max_bytes=byte_limit,
                allowed_base_url=allowed_base_url,
                proxy=proxy,
                verify=verify,
            )
            status = int(
                getattr(response, "status_code", 0)
                or getattr(response, "status", 0)
                or (response.getcode() if callable(getattr(response, "getcode", None)) else 0)
                or 0
            )
            if status not in {200, 206}:
                raise ReturnedUrlVerificationError(f"returned image URL responded with HTTP {status}")
            headers = getattr(response, "headers", {}) or {}
            content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            payload = response.read(byte_limit)
        except ReturnedUrlVerificationError as exc:
            last_error = exc
        except HTTPError as exc:
            last_error = ReturnedUrlVerificationError(f"returned image URL responded with HTTP {exc.code}")
        except URLError as exc:
            last_error = ReturnedUrlVerificationError(f"returned image URL fetch failed: {exc.reason}")
        except _ReturnedUrlResponseTooLarge as exc:
            last_error = ReturnedUrlVerificationError(
                f"returned image URL exceeds the {byte_limit}-byte verification limit"
            )
        except TimeoutError as exc:
            last_error = ReturnedUrlVerificationError("returned image URL fetch timed out")
        else:
            if not payload:
                last_error = ReturnedUrlVerificationError("returned image URL returned empty content")
            elif content_type and not content_type.startswith("image/"):
                last_error = ReturnedUrlVerificationError(
                    f"returned image URL content-type is not image/*: {content_type}"
                )
            elif not _looks_like_image(payload):
                last_error = ReturnedUrlVerificationError("returned image URL content is not a recognized image")
            else:
                return
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if index + 1 < total_attempts:
            time.sleep(min(1.0, 0.2 * (index + 1)))
    raise ReturnedUrlVerificationError(str(last_error or "returned image URL verification failed"))
