from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_PROXY_URL_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
_BEARER_SECRET_RE = re.compile(r"(\bAuthorization\s*:\s*Bearer\s+)([^,\s;}\]]+)", re.IGNORECASE)
_CREDENTIAL_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_])((?:['\"])?(?:password|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"api[_-]?key|claim[_-]?token|service[_-]?token|client[_-]?id|client[_-]?secret|"
    r"authorization|code|state|nonce|ticket|scope|cookie|session|credential)"
    r"(?:['\"])?\s*[:=]\s*)"
    r"(?:['\"]([^'\"]*)['\"]|([^,\s;}\]]+))",
    re.IGNORECASE,
)
_ONE_TIME_CODE_RE = re.compile(
    r"((?:验证码|verification\s+code|one[-\s]?time\s+password|otp)"
    r"\s*(?:[:：=]|为|is)?\s*)(\d{6})\b",
    re.IGNORECASE,
)


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = f"***@{netloc.rsplit('@', 1)[-1]}"
    query = urlencode([
        (key, "***")
        for key, _item in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def redact_register_proxy(value: object) -> str:
    """Redact credentials and query secrets from a configured proxy URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.casefold() not in _PROXY_URL_SCHEMES or not parsed.netloc:
        return raw
    return _redact_url(raw)


def redact_register_log_text(text: object) -> str:
    value = str(text or "")
    if not value:
        return ""
    value = _URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
    value = _BEARER_SECRET_RE.sub(lambda match: f"{match.group(1)}***", value)
    value = _CREDENTIAL_FIELD_RE.sub(
        lambda match: f'{match.group(1)}"{ "***" }"' if match.group(2) is not None else f"{match.group(1)}***",
        value,
    )
    value = _ONE_TIME_CODE_RE.sub(lambda match: f"{match.group(1)}***", value)
    return value


def redact_register_snapshot_inplace(snapshot: Any) -> Any:
    if not isinstance(snapshot, dict):
        return snapshot

    def redact_proxy_values(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if str(key).strip().casefold() == "proxy" and isinstance(item, str):
                    value[key] = redact_register_proxy(item)
                else:
                    redact_proxy_values(item)
        elif isinstance(value, list):
            for item in value:
                redact_proxy_values(item)

    redact_proxy_values(snapshot)
    logs = snapshot.get("logs")
    if not isinstance(logs, list):
        return snapshot
    for index, entry in enumerate(logs):
        if isinstance(entry, dict):
            entry["text"] = redact_register_log_text(entry.get("text"))
        elif isinstance(entry, str):
            logs[index] = redact_register_log_text(entry)
    return snapshot


def redact_register_snapshot(snapshot: Any) -> Any:
    return redact_register_snapshot_inplace(copy.deepcopy(snapshot))
