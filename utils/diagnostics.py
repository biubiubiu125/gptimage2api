from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit


EXCEPTION_DIAGNOSTIC_ATTRS: tuple[tuple[str, str], ...] = (
    ("code", "error_code"),
    ("raw_error", "raw_error"),
    ("upstream_error", "upstream_error"),
    ("upstream_error_type", "upstream_error_type"),
    ("upstream_request_id", "upstream_request_id"),
    ("can_resume_poll", "can_resume_poll"),
    ("raw_upstream_message", "raw_upstream_message"),
    ("raw_upstream_message_len", "raw_upstream_message_len"),
    ("raw_upstream_message_truncated", "raw_upstream_message_truncated"),
    ("upstream_message_preview", "upstream_message_preview"),
    ("upstream_message_len", "upstream_message_len"),
    ("upstream_message_truncated", "upstream_message_truncated"),
    ("tool_invoked", "tool_invoked"),
    ("terminal_message", "terminal_message"),
    ("blocked", "blocked"),
    ("poll_attempts", "poll_attempts"),
    ("poll_timeout_secs", "poll_timeout_secs"),
    ("stream_timeout_secs", "stream_timeout_secs"),
    ("stream_timeout_followup", "stream_timeout_followup"),
    ("last_task_error", "last_task_error"),
    ("last_conversation_snapshot", "last_conversation_snapshot"),
    ("image_attempts", "image_attempts"),
)

_DIAGNOSTIC_SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "authorization",
    "proxy_authorization",
    "proxyauthorization",
    "password",
    "proxy_password",
    "proxypassword",
}

_DIAGNOSTIC_PROXY_KEYS = {
    "proxy",
    "proxy_url",
    "proxyurl",
}

_NON_SECRET_PROXY_VALUES = {
    "default",
    "direct",
    "inherit",
    "system",
}

_DIAGNOSTIC_URL_RE = re.compile(
    r"(?P<url>(?:https?://|/images/|/image-thumbnails/)[^\s<>'\"`]+)",
    re.IGNORECASE,
)


def _strip_diagnostic_url_query(value: str) -> str:
    trailing = ""
    while value and value[-1] in ".,;:!?)]}":
        trailing = value[-1] + trailing
        value = value[:-1]
    if not value:
        return trailing
    if value.startswith("/"):
        clean = value.split("?", 1)[0].split("#", 1)[0]
    else:
        parsed = urlsplit(value)
        clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return clean + trailing


def _sanitize_diagnostic_urls(value: str) -> str:
    return _DIAGNOSTIC_URL_RE.sub(
        lambda match: _strip_diagnostic_url_query(match.group("url")),
        value,
    )


def sanitize_diagnostic_text(
    value: object,
    *,
    sensitive_values: Iterable[object] = (),
    proxy_values: Iterable[object] = (),
    limit: int = 0,
) -> str:
    """Return diagnostic text unchanged. Admin logs keep credentials in plaintext."""
    del sensitive_values, proxy_values
    del limit
    return str(value or "").strip()


def _collect_diagnostic_values(
    value: object,
    sensitive_values: list[object],
    proxy_values: list[object],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in _DIAGNOSTIC_SECRET_KEYS and item not in (None, ""):
                sensitive_values.append(item)
            elif normalized_key in _DIAGNOSTIC_PROXY_KEYS and item not in (None, ""):
                proxy_values.append(item)
            _collect_diagnostic_values(item, sensitive_values, proxy_values)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_diagnostic_values(item, sensitive_values, proxy_values)


def _scrub_diagnostic_value(
    value: object,
    sensitive_values: list[object],
    proxy_values: list[object],
) -> object:
    del sensitive_values, proxy_values
    return value


def scrub_diagnostic_value(
    value: object,
    *,
    sensitive_values: Iterable[object] = (),
    proxy_values: Iterable[object] = (),
) -> object:
    """Return diagnostic payloads unchanged. Admin logs keep credentials in plaintext."""
    del sensitive_values, proxy_values
    return value


def diagnostic_excerpt(value: object, limit: int = 0) -> str:
    """Return the full diagnostic string. Admin logs do not truncate plaintext."""
    del limit
    return str(value or "").strip()


def exception_diagnostic_fields(
    exc: Exception,
    *,
    include_status_code: bool = False,
    string_limit: int = 4000,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    attrs = EXCEPTION_DIAGNOSTIC_ATTRS
    if include_status_code:
        attrs = (("status_code", "status_code"), *attrs)
    for attr, key in attrs:
        if not hasattr(exc, attr):
            continue
        value = getattr(exc, attr)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            value = diagnostic_excerpt(value, string_limit)
        fields[key] = value
    followup = fields.get("stream_timeout_followup")
    if isinstance(followup, dict) and "diagnosis" not in fields:
        fields["diagnosis"] = followup
    return fields
