from __future__ import annotations

import re
from collections.abc import Mapping

from services.image_failure import ImageFailure, public_image_error_message, redact_public_urls

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(^|[_-])("
    r"authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|"
    r"access[-_]?token|refresh[-_]?token|id[-_]?token|api[-_]?key|"
    r"password|secret|token"
    r")($|[_-])"
)
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|access_token|"
    r"refresh_token|id[_-]?token|api[_-]?key|password|secret|token)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
ALLOWED_IMAGE_TRACE_HEADERS = {
    "x-request-id",
    "x-newapi-request-id",
    "x-oneapi-request-id",
    "x-channel-id",
    "x-channel-name",
    "call_id",
}


def _safe_text(value: object, limit: int = 0) -> str:
    text = str(value or "").strip()
    if limit > 0:
        return text[:limit]
    return text


def _is_sensitive_key(key: object) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key or "").strip()))


def sanitize_event_data(value: object, *, _depth: int = 0) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            sanitized[key_text] = sanitize_event_data(
                item,
                _depth=_depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_event_data(item, _depth=_depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[bytes {len(bytes(value))}]"
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def sanitize_trace_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        key_text = str(key or "").strip().lower()
        if key_text not in ALLOWED_IMAGE_TRACE_HEADERS:
            continue
        text = _safe_text(value)
        if text:
            result[key_text] = text
    return result


def sanitize_delivery_url(value: object) -> str:
    """Keep the original delivery URL for admin diagnostics."""
    return str(value or "").strip()


def original_queue_error_message(error: BaseException, failure: ImageFailure) -> str:
    candidates = (
        failure.raw_detail,
        getattr(error, "raw_error", None),
        getattr(error, "raw_upstream_message", None),
        getattr(error, "upstream_error", None),
        str(error or ""),
        failure.code,
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def safe_queue_error_message(error: BaseException, failure: ImageFailure) -> str:
    message = public_image_error_message(failure, error).strip()
    if not message:
        message = "图片生成失败，请稍后重试。"
    message = redact_public_urls(message)
    message = _BEARER_RE.sub("Bearer [redacted]", message)
    message = _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", message)
    return message
