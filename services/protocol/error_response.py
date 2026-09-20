from __future__ import annotations

from typing import Any, Mapping

from fastapi.responses import JSONResponse

MODELS_UNAVAILABLE_PUBLIC_MESSAGE = "模型列表暂时无法加载，请稍后重试。"
REQUEST_FAILED_PUBLIC_MESSAGE = "请求失败。"
UPSTREAM_UNAVAILABLE_PUBLIC_MESSAGE = "上游服务暂时不可用，请稍后重试。"
BACKUP_RESTORE_IN_PROGRESS_PUBLIC_MESSAGE = "服务正在恢复备份，请等待进程重启后再试。"
VALIDATION_ERROR_PUBLIC_MESSAGE = "请求参数无效。"
NOT_FOUND_PUBLIC_MESSAGE = "找不到该接口。"
METHOD_NOT_ALLOWED_PUBLIC_MESSAGE = "请求方法不被允许。"
INTERNAL_SERVER_ERROR_PUBLIC_MESSAGE = "服务器内部错误。"

_STARLETTE_PUBLIC_MESSAGES = {
    "Not Found": NOT_FOUND_PUBLIC_MESSAGE,
    "Method Not Allowed": METHOD_NOT_ALLOWED_PUBLIC_MESSAGE,
    "Internal Server Error": INTERNAL_SERVER_ERROR_PUBLIC_MESSAGE,
}


def localize_starlette_detail(detail: object) -> object:
    if isinstance(detail, str):
        return _STARLETTE_PUBLIC_MESSAGES.get(detail, detail)
    return detail


def wrap_non_openai_http_detail(path: str, detail: object, status_code: int) -> object:
    localized = localize_starlette_detail(detail)
    if isinstance(localized, list):
        localized = localize_validation_errors(localized)
    path_text = str(path or "")
    if path_text != "/api/image-tasks" and not path_text.startswith("/api/image-tasks/"):
        return localized
    stage = "请求失败"
    code = int(status_code)
    if code == 401:
        stage = "认证失败"
    elif code == 403:
        stage = "权限不足"
    elif code in {400, 422}:
        stage = "请求参数"
    elif code == 404:
        stage = "任务不存在"
    elif code == 405:
        stage = "请求方法"
    elif code == 429:
        stage = "请求过快"
    elif code >= 500:
        stage = "内部错误"
    from services.image_failure import wrap_public_http_detail

    return wrap_public_http_detail(localized, stage=stage)


_VALIDATION_TYPE_REASONS = {
    "missing": "缺少必填值",
    "missing_argument": "缺少必填值",
    "string_type": "必须是字符串",
    "string_too_short": "长度过短",
    "string_too_long": "长度过长",
    "int_type": "必须是整数",
    "int_parsing": "必须是整数",
    "float_type": "必须是数字",
    "float_parsing": "必须是数字",
    "bool_type": "必须是布尔值",
    "bool_parsing": "必须是布尔值",
    "list_type": "必须是数组",
    "dict_type": "必须是对象",
    "json_invalid": "JSON 无效",
    "json_type": "必须是有效 JSON",
    "extra_forbidden": "不支持该参数",
    "literal_error": "取值无效",
    "enum": "取值无效",
    "too_short": "长度过短",
    "too_long": "长度过长",
    "greater_than": "超出允许范围",
    "greater_than_equal": "超出允许范围",
    "less_than": "超出允许范围",
    "less_than_equal": "超出允许范围",
    "value_error": "取值无效",
    "uuid_parsing": "取值无效",
    "url_parsing": "取值无效",
    "datetime_parsing": "取值无效",
    "date_parsing": "取值无效",
    "time_parsing": "取值无效",
    "bytes_type": "必须是二进制数据",
    "model_type": "取值无效",
    "dataclass_type": "取值无效",
}
_VALIDATION_LOCATION_SKIP = {"body", "query", "path", "header", "cookie"}


def _message_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    message = value.get("message")
    if isinstance(message, str) and message:
        return message
    return _message_from_value(value.get("error"))


def _is_validation_error_item(item: object) -> bool:
    return isinstance(item, Mapping) and ("loc" in item or "type" in item)


def _validation_location(item: Mapping[str, Any]) -> str:
    loc = item.get("loc") or ()
    if not isinstance(loc, (list, tuple)):
        loc = (loc,)
    return ".".join(
        str(part) for part in loc if str(part) not in _VALIDATION_LOCATION_SKIP
    )


def _validation_reason(item: Mapping[str, Any]) -> str:
    raw = str(item.get("type") or "").strip().lower()
    key = raw.split(".", 1)[0] if raw else ""
    return _VALIDATION_TYPE_REASONS.get(key, "取值无效")


def validation_item_public_message(
    item: Mapping[str, Any],
    *,
    include_location: bool = True,
) -> str:
    reason = _validation_reason(item)
    location = _validation_location(item)
    if include_location and location:
        return f"参数 {location} {reason}。"
    return f"{reason}。"


def public_validation_error_message(detail: object) -> str:
    if not isinstance(detail, list):
        return VALIDATION_ERROR_PUBLIC_MESSAGE
    messages = [
        validation_item_public_message(item)
        for item in detail
        if _is_validation_error_item(item)
    ]
    return "；".join(messages) or VALIDATION_ERROR_PUBLIC_MESSAGE


def localize_validation_errors(errors: object) -> object:
    if not isinstance(errors, list):
        return errors
    localized: list[object] = []
    for item in errors:
        if not _is_validation_error_item(item):
            localized.append(item)
            continue
        copy = dict(item)
        copy["msg"] = validation_item_public_message(item, include_location=False)
        localized.append(copy)
    return localized


def error_message_from_detail(detail: object) -> str:
    if isinstance(detail, list):
        if any(_is_validation_error_item(item) for item in detail):
            return public_validation_error_message(detail)
        messages = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in item.get("loc", []) if part != "body")
            message = str(item.get("msg") or "").strip()
            if location and message:
                messages.append(f"{location}: {message}")
            elif message:
                messages.append(message)
        return "; ".join(messages)
    if isinstance(detail, dict):
        message = _message_from_value(detail.get("error")) or _message_from_value(detail)
        if message:
            return message
    text = str(detail or "").strip()
    return str(_STARLETTE_PUBLIC_MESSAGES.get(text, text))


def _default_error_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 429:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "server_error"


def _default_error_code(status_code: int) -> str:
    if status_code == 401:
        return "invalid_api_key"
    if status_code == 403:
        return "permission_denied"
    if status_code == 429:
        return "rate_limit_exceeded"
    if 400 <= status_code < 500:
        return "bad_request"
    return "upstream_error"


def _public_protocol_http_message(message: str, status_code: int) -> str:
    from services.image_failure import public_http_chinese_error

    text = str(message or "").strip() or REQUEST_FAILED_PUBLIC_MESSAGE
    stage = "请求失败"
    if text == NOT_FOUND_PUBLIC_MESSAGE or status_code == 404:
        stage = "接口不存在"
    elif text == METHOD_NOT_ALLOWED_PUBLIC_MESSAGE or status_code == 405:
        stage = "请求方法"
    elif status_code == 401:
        stage = "认证失败"
    elif status_code == 403:
        stage = "权限不足"
    elif status_code == 429:
        stage = "请求过快"
    elif text == MODELS_UNAVAILABLE_PUBLIC_MESSAGE:
        stage = "模型列表"
    elif text == BACKUP_RESTORE_IN_PROGRESS_PUBLIC_MESSAGE:
        stage = "备份恢复"
    elif text == VALIDATION_ERROR_PUBLIC_MESSAGE or status_code in {400, 422}:
        stage = "请求参数"
    elif status_code >= 500:
        stage = "内部错误"
    return public_http_chinese_error(stage=stage, reason=text)


def openai_error_payload(
    detail: object,
    status_code: int,
    *,
    error_type: str | None = None,
    code: object | None = None,
    param: object | None = None,
) -> dict[str, Any]:
    error_detail = detail.get("error") if isinstance(detail, dict) else None
    if isinstance(error_detail, dict):
        return {
            "error": {
                "message": _public_protocol_http_message(
                    error_message_from_detail(error_detail) or REQUEST_FAILED_PUBLIC_MESSAGE,
                    status_code,
                ),
                "type": str(error_detail.get("type") or error_type or _default_error_type(status_code)),
                "param": error_detail.get("param", param),
                "code": error_detail.get("code", code if code is not None else _default_error_code(status_code)),
            }
        }
    return {
        "error": {
            "message": _public_protocol_http_message(
                error_message_from_detail(detail) or REQUEST_FAILED_PUBLIC_MESSAGE,
                status_code,
            ),
            "type": error_type or _default_error_type(status_code),
            "param": param,
            "code": code if code is not None else _default_error_code(status_code),
        }
    }


def openai_error_response(
    detail: object,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    error_type: str | None = None,
    code: object | None = None,
    param: object | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_payload(detail, status_code, error_type=error_type, code=code, param=param),
        headers=headers,
    )


def anthropic_error_response(
    detail: object,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error_type = "api_error" if status_code >= 500 else _default_error_type(status_code)
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": _public_protocol_http_message(
                    error_message_from_detail(detail) or REQUEST_FAILED_PUBLIC_MESSAGE,
                    status_code,
                ),
            },
        },
        headers=headers,
    )
