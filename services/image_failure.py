from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from curl_cffi.requests import exceptions as curl_exceptions

from utils.helper import UpstreamHTTPError


@dataclass(frozen=True)
class FailurePolicy:
    scope: str
    capability: str | None
    retryable: bool
    status_code: int
    error_type: str
    verify_account: bool = False


@dataclass(frozen=True)
class ImageFailure:
    code: str
    scope: str
    capability: str | None
    retryable: bool
    retry_after: int | None
    status_code: int
    error_type: str
    verify_account: bool = False
    raw_detail: Any = field(default=None, compare=False, repr=False)
    public_detail: str = field(default="", compare=False, repr=False)

    @property
    def outcome(self) -> str:
        return "text" if self.status_code == 400 else "failure"

    @property
    def switch_account(self) -> bool:
        return self.outcome == "failure"

    @property
    def account_failure(self) -> bool:
        return self.verify_account

    def with_raw_detail(self, raw_detail: Any) -> "ImageFailure":
        return replace(self, raw_detail=raw_detail)

    def with_public_detail(self, public_detail: Any) -> "ImageFailure":
        return replace(self, public_detail=_safe_public_text(public_detail))

    def diagnostic_fields(self) -> dict[str, Any]:
        return {
            "failure_code": self.code,
            "failure_scope": self.scope,
            "failure_capability": self.capability,
            "failure_retryable": self.retryable,
            "failure_account_failure": self.verify_account,
            "failure_retry_after": self.retry_after,
            "status_code": self.status_code,
            "error_type": self.error_type,
        }


FAILURE_POLICIES: dict[str, FailurePolicy] = {
    "upstream_error": FailurePolicy(
        "transient", None, True, 502, "server_error",
    ),
    "conversation_not_ready": FailurePolicy(
        "transient", "image_generation", True, 409, "server_error",
    ),
    "internal_error": FailurePolicy(
        "internal", None, False, 500, "server_error",
    ),
    "durable_context_required": FailurePolicy(
        "internal", None, False, 500, "server_error",
    ),
    "image_task_pending": FailurePolicy(
        "internal", None, True, 409, "server_error",
    ),
    "invalid_image_result": FailurePolicy(
        "delivery", None, True, 502, "server_error",
    ),
    "image_task_not_found": FailurePolicy(
        "request", None, False, 404, "invalid_request_error",
    ),
    "image_job_failed": FailurePolicy(
        "internal", None, False, 500, "server_error",
    ),
    "image_url_unreachable": FailurePolicy(
        "delivery", None, True, 502, "server_error",
    ),
    "image_claim_timeout": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "local_artifact_unavailable": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "worker_local_recovery_unavailable": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "recovery_account_unavailable": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "queue_timeout": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "legacy_interrupted": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "legacy_failed": FailurePolicy(
        "internal", None, False, 500, "server_error",
    ),
    "image_queue_unavailable": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "image_queue_resource_pressure": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "image_queue_storage_full": FailurePolicy(
        "internal", None, True, 503, "server_error",
    ),
    "upstream_unavailable": FailurePolicy(
        "transient", None, True, 502, "server_error",
    ),
    "upstream_challenge_required": FailurePolicy(
        "transient", None, True, 503, "server_error",
    ),
    "upstream_connection_failed": FailurePolicy(
        "transient", None, True, 502, "server_error",
    ),
    "upstream_connection_timeout": FailurePolicy(
        "transient", None, True, 504, "server_error",
    ),
    "upstream_rate_limited": FailurePolicy(
        "transient", "image_generation", False, 429, "rate_limit_error",
        verify_account=True,
    ),
    "image_poll_timeout": FailurePolicy(
        "transient", "image_generation", True, 502, "server_error",
    ),
    "image_stream_timeout": FailurePolicy(
        "transient", "image_generation", True, 502, "server_error",
    ),
    "image_stream_interrupted": FailurePolicy(
        "transient", "image_generation", True, 502, "server_error",
    ),
    "image_tool_error": FailurePolicy(
        "account", "image_generation", False, 502, "server_error",
        verify_account=True,
    ),
    "image_quota_exhausted": FailurePolicy(
        "account", "image_generation", False, 429, "insufficient_quota",
        verify_account=True,
    ),
    "file_upload_throttled": FailurePolicy(
        "account", "file_upload", True, 429, "rate_limit_error",
        verify_account=True,
    ),
    "auth_invalid": FailurePolicy(
        "account", "auth", False, 401, "authentication_error",
        verify_account=True,
    ),
    "content_policy_violation": FailurePolicy(
        "request", None, False, 400, "invalid_request_error",
    ),
    "invalid_image_input": FailurePolicy(
        "request", None, False, 400, "invalid_request_error",
    ),
    "upstream_text_reply": FailurePolicy(
        "request", None, False, 400, "invalid_request_error",
    ),
    "conversation_mode_blocked": FailurePolicy(
        "request", None, False, 400, "invalid_request_error",
    ),
    "no_image_generated": FailurePolicy(
        "request", None, False, 502, "server_error",
        verify_account=True,
    ),
    "unsupported_model": FailurePolicy(
        "request", None, False, 400, "invalid_request_error",
    ),
    "image_download_failed": FailurePolicy(
        "delivery", None, True, 502, "server_error",
    ),
    "task_interrupted": FailurePolicy(
        "request", None, False, 503, "server_error",
    ),
    "no_available_account": FailurePolicy(
        "transient", None, True, 503, "server_error",
    ),
    "insufficient_quota": FailurePolicy(
        "account", "image_generation", False, 429, "insufficient_quota",
        verify_account=True,
    ),
}


FAILURE_CODE_ALIASES = {
    "connection_failed": "upstream_connection_failed",
    "connection_timeout": "upstream_connection_timeout",
    "image_stream_interrupted": "image_stream_interrupted",
    "invalid_access_token": "auth_invalid",
    "moderation_blocked": "content_policy_violation",
    "quota_exhausted": "image_quota_exhausted",
    "rate_limit_exceeded": "upstream_rate_limited",
    "safety_blocked": "content_policy_violation",
    "token_invalid": "auth_invalid",
    "token_invalidated": "auth_invalid",
    "token_revoked": "auth_invalid",
    "unsupported_image_model": "unsupported_model",
    "upstream_timeout": "image_poll_timeout",
}

RATE_LIMIT_FAILURE_CODES = frozenset({
    "429",
    "file_upload_throttled",
    "image_quota_exhausted",
    "insufficient_quota",
    "limited",
    "quota_exhausted",
    "rate_limit",
    "rate_limit_exceeded",
    "rate_limited",
    "upstream_rate_limited",
    "限流",
})

TEXT_REVIEW_FAILURE_CODES = frozenset(
    code
    for code, policy in FAILURE_POLICIES.items()
    if policy.status_code == 400
)

FAILED_STATUSES = frozenset({"error", "fail", "failed", "limited", "rate_limited", "限流"})


def is_structured_failure(
    *,
    status: Any = None,
    error: Any = None,
    error_code: Any = None,
    failure_code: Any = None,
) -> bool:
    return str(status or "").strip().lower() in FAILED_STATUSES or any(
        value not in (None, "")
        for value in (error, error_code, failure_code)
    )


def is_rate_limit_failure_code(value: Any) -> bool:
    return str(value or "").strip().lower() in RATE_LIMIT_FAILURE_CODES


def is_text_review_failure_code(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    normalized = FAILURE_CODE_ALIASES.get(normalized, normalized)
    return normalized in TEXT_REVIEW_FAILURE_CODES


_CONVERSATION_MODE_BLOCKED_MARKERS = (
    "无法调用图片生成工具",
    "请切换到普通聊天",
    "cannot use the image generation tool",
    "switch to a regular chat",
    "switch to regular chat",
    "conversation_mode_blocked",
)


def looks_like_conversation_mode_blocked(value: Any) -> bool:
    text = _safe_public_text(value) or str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(
        marker in text or marker.lower() in lowered
        for marker in _CONVERSATION_MODE_BLOCKED_MARKERS
    )


def should_switch_image_account(error_code: object) -> bool:
    """Return True when a failed job should retry on a different upstream account."""

    code = str(error_code or "").strip().lower()
    code = FAILURE_CODE_ALIASES.get(code, code)
    policy = FAILURE_POLICIES.get(code)
    if policy is None:
        return False
    return bool(policy.verify_account) and int(policy.status_code) != 400


def image_failure(
    code: str | None,
    *,
    retry_after: int | None = None,
    raw_detail: Any = None,
) -> ImageFailure:
    normalized = str(code or "upstream_error").strip().lower()
    normalized = FAILURE_CODE_ALIASES.get(normalized, normalized)
    if normalized not in FAILURE_POLICIES:
        normalized = "upstream_error"
    policy = FAILURE_POLICIES[normalized]
    return ImageFailure(
        code=normalized,
        scope=policy.scope,
        capability=policy.capability,
        retryable=policy.retryable,
        retry_after=retry_after,
        status_code=policy.status_code,
        error_type=policy.error_type,
        verify_account=policy.verify_account,
        raw_detail=raw_detail,
    )


IMAGE_TIMEOUT_PUBLIC_MESSAGE = "图片生成超时，请稍后重试。"
IMAGE_TOOL_ERROR_PUBLIC_MESSAGE = "图片生成工具出错，请稍后重试。"
IMAGE_QUOTA_PUBLIC_MESSAGE = "当前没有可用的图片生成额度。"
IMAGE_QUEUE_UNAVAILABLE_PUBLIC_MESSAGE = "图片队列暂时不可用，请稍后重试。"
IMAGE_QUEUE_STORAGE_FULL_PUBLIC_MESSAGE = "图片队列存储已满，请稍后重试。"
CONVERSATION_MODE_BLOCKED_PUBLIC_MESSAGE = "无法在当前会话中调用图片生成工具，请稍后重试。"
UNSUPPORTED_MODEL_PUBLIC_MESSAGE = "当前模型不支持图片生成。"
IMAGE_INPUT_INVALID_PUBLIC_MESSAGE = "参考图无效，请更换后重试。"
NO_IMAGE_GENERATED_PUBLIC_MESSAGE = "未生成图片，请稍后重试。"
QUOTA_COMMIT_FAILED_PUBLIC_MESSAGE = "图片任务已创建，但额度状态未能提交；请查询任务或使用相同幂等键重试。"
EDITABLE_QUOTA_COMMIT_FAILED_PUBLIC_MESSAGE = "可编辑文件任务已创建，但额度状态未能提交；请查询任务或使用相同 client_task_id 重试。"
IDEMPOTENCY_CONFLICT_PUBLIC_MESSAGE = "该请求与已有图片任务冲突，请更换幂等键或查询已有任务。"
EDITABLE_IDEMPOTENCY_KEY_REQUIRED_PUBLIC_MESSAGE = "可编辑文件任务必须提供 Idempotency-Key、X-NewAPI-Request-Id、X-OneAPI-Request-Id 或 client_task_id。"
TASK_STATE_CONFLICT_PUBLIC_MESSAGE = "当前任务状态不允许该操作。"
TASK_ACK_REQUIRES_COMPLETED_PUBLIC_MESSAGE = "只有成功或部分完成的图片结果才能确认。"
TASK_RESULT_NO_LONGER_AVAILABLE_PUBLIC_MESSAGE = "可交付的图片结果已不可用。"
IMAGE_QUEUE_RESTORE_REQUIRES_EMPTY_DATABASE_PUBLIC_MESSAGE = "图片队列恢复要求数据库为空。"


def image_queue_http_message(code: str = "") -> str:
    if str(code or "").strip() == "image_queue_storage_full":
        return public_http_chinese_error(
            stage="存储已满",
            reason=IMAGE_QUEUE_STORAGE_FULL_PUBLIC_MESSAGE,
        )
    return public_http_chinese_error(
        stage="队列不可用",
        reason=IMAGE_QUEUE_UNAVAILABLE_PUBLIC_MESSAGE,
    )


IMAGE_TASK_PENDING_PUBLIC_MESSAGE = "图片任务仍在执行，请继续查询任务状态。"
IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE = "已保存的图片结果不可用，请稍后重试。"
IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE = "找不到该图片任务。"
IMAGE_DELIVERY_FAILED_PUBLIC_MESSAGE = "生成的图片无法交付，请稍后重试。"

_DIRECT_PUBLIC_TEXT_CODES = frozenset({
    "content_policy_violation",
    "invalid_image_input",
    "upstream_text_reply",
})

CONTENT_POLICY_VIOLATION_PUBLIC_MESSAGE = "内容未通过审核，请修改后重试。"
RATE_LIMITED_PUBLIC_MESSAGE = "请求过于频繁，请稍后重试。"
AUTH_INVALID_PUBLIC_MESSAGE = "账号凭证无效，请稍后重试。"
NO_AVAILABLE_ACCOUNT_PUBLIC_MESSAGE = "当前没有可用的上游账号。"
TASK_INTERRUPTED_PUBLIC_MESSAGE = "图片任务已中断，请稍后重试。"
DURABLE_CONTEXT_REQUIRED_PUBLIC_MESSAGE = "图片生成必须走持久化图片队列。"
_PUBLIC_FOUR_SEGMENT_PREFIX = "对话生图失败【"

_PUBLIC_IMAGE_ERROR_SPECS: dict[str, tuple[str, str]] = {
    "upstream_error": ("工具出错", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "conversation_not_ready": ("会话未就绪", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "internal_error": ("内部错误", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "durable_context_required": ("必须走队列", DURABLE_CONTEXT_REQUIRED_PUBLIC_MESSAGE),
    "image_task_pending": ("任务进行中", IMAGE_TASK_PENDING_PUBLIC_MESSAGE),
    "invalid_image_result": ("结果不可用", IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE),
    "image_task_not_found": ("任务不存在", IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE),
    "image_job_failed": ("任务失败", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "image_url_unreachable": ("交付失败", IMAGE_DELIVERY_FAILED_PUBLIC_MESSAGE),
    "image_claim_timeout": ("超时", IMAGE_TIMEOUT_PUBLIC_MESSAGE),
    "local_artifact_unavailable": ("结果不可用", IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE),
    "worker_local_recovery_unavailable": ("结果不可用", IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE),
    "recovery_account_unavailable": ("结果不可用", IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE),
    "queue_timeout": ("超时", IMAGE_TIMEOUT_PUBLIC_MESSAGE),
    "legacy_interrupted": ("任务中断", TASK_INTERRUPTED_PUBLIC_MESSAGE),
    "legacy_failed": ("任务失败", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "image_queue_unavailable": ("队列不可用", IMAGE_QUEUE_UNAVAILABLE_PUBLIC_MESSAGE),
    "image_queue_resource_pressure": ("队列不可用", IMAGE_QUEUE_UNAVAILABLE_PUBLIC_MESSAGE),
    "image_queue_storage_full": ("存储已满", IMAGE_QUEUE_STORAGE_FULL_PUBLIC_MESSAGE),
    "upstream_unavailable": ("上游不可用", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "upstream_challenge_required": ("上游挑战", IMAGE_QUEUE_UNAVAILABLE_PUBLIC_MESSAGE),
    "upstream_connection_failed": ("连接失败", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "upstream_connection_timeout": ("超时", IMAGE_TIMEOUT_PUBLIC_MESSAGE),
    "upstream_rate_limited": ("限流", RATE_LIMITED_PUBLIC_MESSAGE),
    "image_poll_timeout": ("超时", IMAGE_TIMEOUT_PUBLIC_MESSAGE),
    "image_stream_timeout": ("超时", IMAGE_TIMEOUT_PUBLIC_MESSAGE),
    "image_stream_interrupted": ("工具出错", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "image_tool_error": ("工具出错", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "image_quota_exhausted": ("额度不足", IMAGE_QUOTA_PUBLIC_MESSAGE),
    "file_upload_throttled": ("限流", RATE_LIMITED_PUBLIC_MESSAGE),
    "auth_invalid": ("鉴权失败", AUTH_INVALID_PUBLIC_MESSAGE),
    "content_policy_violation": ("内容审核", CONTENT_POLICY_VIOLATION_PUBLIC_MESSAGE),
    "invalid_image_input": ("参考图无效", IMAGE_INPUT_INVALID_PUBLIC_MESSAGE),
    "upstream_text_reply": ("上游返回文本", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    "conversation_mode_blocked": ("会话限制", CONVERSATION_MODE_BLOCKED_PUBLIC_MESSAGE),
    "no_image_generated": ("未出图", NO_IMAGE_GENERATED_PUBLIC_MESSAGE),
    "unsupported_model": ("模型不支持", UNSUPPORTED_MODEL_PUBLIC_MESSAGE),
    "image_download_failed": ("交付失败", IMAGE_DELIVERY_FAILED_PUBLIC_MESSAGE),
    "task_interrupted": ("任务中断", TASK_INTERRUPTED_PUBLIC_MESSAGE),
    "no_available_account": ("无可用账号", NO_AVAILABLE_ACCOUNT_PUBLIC_MESSAGE),
    "insufficient_quota": ("额度不足", IMAGE_QUOTA_PUBLIC_MESSAGE),
}

def _is_structured_text_payload(text: str) -> bool:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
    try:
        json.loads(candidate)
    except (TypeError, ValueError):
        return False
    return True


def _is_structured_failure_code(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in (
        FAILURE_POLICIES.keys()
        | FAILURE_CODE_ALIASES.keys()
        | RATE_LIMIT_FAILURE_CODES
        | FAILED_STATUSES
    )


_PUBLIC_URL_RE = re.compile(
    r"(?i)\b(?:https?|socks5h?|socks5|postgres(?:ql)?(?:\+\w+)?)://[^\s\"'<>]+"
)
_PUBLIC_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_PUBLIC_SECRET_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|access_token|"
    r"refresh_token|id[_-]?token|api[_-]?key|password|secret|token)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_public_urls(text: str) -> str:
    return _PUBLIC_URL_RE.sub("[url redacted]", str(text or ""))


def redact_public_secrets(text: str) -> str:
    value = redact_public_urls(str(text or ""))
    value = _PUBLIC_BEARER_RE.sub("Bearer [redacted]", value)
    return _PUBLIC_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", value)


def is_formatted_public_chinese_error(text: str) -> bool:
    return str(text or "").startswith(_PUBLIC_FOUR_SEGMENT_PREFIX)


def format_public_chinese_error(*, stage: str, reason: str, original: str = "") -> str:
    original_text = redact_public_secrets(original).strip()
    if is_formatted_public_chinese_error(original_text):
        return original_text
    text = f"{_PUBLIC_FOUR_SEGMENT_PREFIX}{stage}】：{reason}".rstrip()
    if original_text and original_text not in text:
        text = f"{text} 原文「{original_text}」"
    return text


def public_error_original(text: str) -> str:
    value = str(text or "").strip()
    marker = " 原文「"
    if is_formatted_public_chinese_error(value) and marker in value and value.endswith("」"):
        return value.rsplit(marker, 1)[1][:-1]
    return value


def public_error_reason(text: str) -> str:
    value = str(text or "").strip()
    if not is_formatted_public_chinese_error(value):
        return value
    _, sep, rest = value.partition("】：")
    if not sep:
        return value
    marker = " 原文「"
    if marker in rest:
        rest = rest.split(marker, 1)[0]
    return rest.strip()


def public_http_chinese_error(*, stage: str, reason: str, original: str = "") -> str:
    reason_text = str(reason or "").strip() or "请求失败。"
    if is_formatted_public_chinese_error(reason_text):
        return reason_text
    return format_public_chinese_error(stage=stage, reason=reason_text, original=original)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def wrap_public_http_detail(detail: object, *, stage: str = "请求失败") -> object:
    if isinstance(detail, list):
        return [wrap_public_http_detail(item, stage=stage) for item in detail]
    if isinstance(detail, dict):
        wrapped = dict(detail)
        message = wrapped.get("message")
        if isinstance(message, str) and message.strip():
            wrapped["message"] = public_http_chinese_error(stage=stage, reason=message)
        error = wrapped.get("error")
        if isinstance(error, str) and error.strip() and _has_cjk(error):
            wrapped["error"] = public_http_chinese_error(stage=stage, reason=error)
        msg = wrapped.get("msg")
        if isinstance(msg, str) and msg.strip():
            wrapped["msg"] = public_http_chinese_error(stage=stage, reason=msg)
        return wrapped
    if isinstance(detail, str) and detail.strip():
        return public_http_chinese_error(stage=stage, reason=detail)
    return detail


IMAGE_URL_FETCH_FAILED_PUBLIC_MESSAGE = "参考图下载失败。"


def public_image_url_fetch_error_message(_detail: object = None) -> str:
    return public_http_chinese_error(
        stage="参考图下载",
        reason=IMAGE_URL_FETCH_FAILED_PUBLIC_MESSAGE,
    )


def _safe_public_text(value: Any) -> str:
    if isinstance(value, Mapping):
        error = value.get("error")
        candidates = [
            error.get("message") if isinstance(error, Mapping) else None,
            value.get("message"),
            value.get("detail"),
            value.get("error_description"),
            error if isinstance(error, str) else None,
        ]
        for candidate in candidates:
            if text := _safe_public_text(candidate):
                return text
        return ""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if _is_structured_text_payload(text) or _is_structured_failure_code(text):
        return ""
    redacted = redact_public_urls(text).strip()
    if not redacted.replace("[url redacted]", "").strip():
        return ""
    return redacted


def _public_upstream_text(
    failure: ImageFailure,
    error: BaseException | None = None,
) -> str:
    candidates: list[Any] = []
    if failure.public_detail:
        candidates.append(failure.public_detail)
    if error is not None:
        candidates.append(getattr(error, "raw_upstream_message", None))
        candidates.append(getattr(error, "raw_error", None))
    candidates.append(failure.raw_detail)
    for candidate in candidates:
        if text := _safe_public_text(candidate):
            return text
    return ""


def public_image_error_message(
    failure: ImageFailure,
    error: BaseException | None = None,
) -> str:
    stage, reason = _PUBLIC_IMAGE_ERROR_SPECS.get(
        failure.code,
        ("工具出错", IMAGE_TOOL_ERROR_PUBLIC_MESSAGE),
    )
    return format_public_chinese_error(
        stage=stage,
        reason=reason,
        original=_public_upstream_text(failure, error),
    )


class ImageFailureError(RuntimeError):
    failure_code = "upstream_error"

    def __init__(
        self,
        message: str = "",
        *,
        failure: ImageFailure | None = None,
        retry_after: int | None = None,
    ) -> None:
        raw_message = str(message or "").strip()
        self.failure = failure or image_failure(
            self.failure_code,
            retry_after=retry_after,
            raw_detail=raw_message,
        )
        super().__init__(raw_message or public_image_error_message(self.failure))


class InvalidAccessTokenError(ImageFailureError):
    failure_code = "auth_invalid"


class ImagePollTimeoutError(ImageFailureError):
    failure_code = "image_poll_timeout"


class ImageDownloadError(ImageFailureError):
    failure_code = "image_download_failed"


class ImageGenerationError(ImageFailureError):
    def __init__(
        self,
        message: str = "",
        code: str | None = None,
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        raw_error: str | None = None,
        upstream_error: str = "",
        raw_upstream_message: str = "",
        failure: ImageFailure | None = None,
        image_attempts: list[dict[str, Any]] | None = None,
        task_id: str = "",
    ) -> None:
        raw_message = str(message or "").strip()
        resolved = failure or image_failure(code, raw_detail=raw_error or raw_message)
        if (
            resolved.raw_detail is None
            and raw_message
            and resolved.code in _DIRECT_PUBLIC_TEXT_CODES
        ):
            resolved = resolved.with_raw_detail(raw_message)
        super().__init__(raw_message, failure=resolved)
        self.status_code = resolved.status_code
        self.error_type = resolved.error_type
        self.code = resolved.code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.task_id = str(task_id or "").strip()
        self.raw_error = raw_message if raw_error is None else str(raw_error or "").strip()
        self.upstream_error = upstream_error
        self.raw_upstream_message = raw_upstream_message
        self.image_attempts = [dict(item) for item in image_attempts or [] if isinstance(item, Mapping)]

    @property
    def public_error(self) -> str:
        current = str(self.args[0] if self.args else "").strip()
        if is_formatted_public_chinese_error(current):
            return redact_public_secrets(current)
        return public_image_error_message(self.failure, self)

    def to_openai_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.public_error,
            "type": self.error_type,
            "param": self.param,
            "code": self.code,
        }
        if self.task_id:
            error["task_id"] = self.task_id
        return {"error": error}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _structured_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key in ("code", "error_code", "failure_code", "type", "error"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    codes.add(candidate.strip().lower())
            pending.extend(
                child
                for child in current.values()
                if isinstance(child, (Mapping, list, tuple))
            )
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return codes


def structured_upstream_codes(value: Any) -> set[str]:
    """Return normalized error codes carried by structured upstream fields."""
    return _structured_codes(value)


QUOTA_CODES = {"insufficient_quota", "quota_exhausted", "image_quota_exhausted"}
AUTH_CODES = {"invalid_access_token", "token_invalid", "token_invalidated", "token_revoked"}
POLICY_CODES = {"content_policy_violation", "moderation_blocked", "safety_blocked"}


def _failure_priority(code: str) -> int:
    normalized = FAILURE_CODE_ALIASES.get(str(code or "").strip().lower(), str(code or "").strip().lower())
    if normalized == "auth_invalid":
        return 8
    if normalized in {"image_quota_exhausted", "insufficient_quota"}:
        return 7
    if normalized in {"file_upload_throttled", "upstream_rate_limited"}:
        return 6
    if normalized == "image_download_failed":
        return 5
    if normalized in TEXT_REVIEW_FAILURE_CODES and normalized != "upstream_text_reply":
        return 4
    if normalized == "image_tool_error":
        return 3
    if normalized == "upstream_text_reply":
        return 2
    if normalized in {
        "image_poll_timeout",
        "image_stream_interrupted",
        "image_stream_timeout",
        "task_interrupted",
        "upstream_connection_failed",
        "upstream_connection_timeout",
        "upstream_unavailable",
    }:
        return 1
    return 0


def image_failure_priority(failure: ImageFailure) -> int:
    """Return the shared ordering used when several image failures compete."""
    return _failure_priority(failure.code)


def _classify_structured_failure_codes(
    codes: Any,
    *,
    retry_after: int | None = None,
    raw_detail: Any = None,
) -> ImageFailure | None:
    normalized_codes = {
        str(code or "").strip().lower()
        for code in (codes if isinstance(codes, (set, frozenset, list, tuple)) else (codes,))
        if str(code or "").strip()
    }
    known_codes: set[str] = set()
    if normalized_codes.intersection(POLICY_CODES):
        known_codes.add("content_policy_violation")
    if normalized_codes.intersection(AUTH_CODES):
        known_codes.add("auth_invalid")
    if normalized_codes.intersection(QUOTA_CODES):
        known_codes.add("image_quota_exhausted")
    known_codes.update(
        FAILURE_CODE_ALIASES.get(code, code)
        for code in normalized_codes
        if code not in POLICY_CODES | AUTH_CODES | QUOTA_CODES
        and FAILURE_CODE_ALIASES.get(code, code) in FAILURE_POLICIES
    )
    if any(is_rate_limit_failure_code(code) for code in normalized_codes):
        known_codes.add("upstream_rate_limited")
    if known_codes:
        selected_code = max(known_codes, key=lambda code: (_failure_priority(code), code))
        return image_failure(selected_code, retry_after=retry_after, raw_detail=raw_detail)
    return None


def classify_upstream_http_error(exc: UpstreamHTTPError) -> ImageFailure:
    codes = _structured_codes(exc.body)
    context = str(exc.context or "").strip().lower()
    context_path = context.split("?", 1)[0].rstrip("/")
    status_code = int(exc.status_code)
    retry_after = exc.retry_after
    credential_scope = str(getattr(exc, "credential_scope", "account") or "account")
    structured_failure = _classify_structured_failure_codes(
        codes,
        retry_after=retry_after,
        raw_detail=exc.body,
    )

    # The real HTTP status owns the text/failure boundary. Structured fields
    # may refine the reason, but must never turn a non-400 response into text
    # or turn a real 400 response into an account failure.
    if status_code == 400:
        failure = (
            structured_failure
            if structured_failure is not None and structured_failure.status_code == 400
            else image_failure("invalid_image_input", raw_detail=exc.body)
        )
        if credential_scope != "account":
            failure = replace(
                failure,
                scope="delivery" if credential_scope == "signed_asset" else failure.scope,
            )
        return failure

    if credential_scope != "account":
        if credential_scope == "signed_asset" and "download" in context:
            failure = image_failure(
                "image_download_failed",
                retry_after=retry_after,
                raw_detail=exc.body,
            )
        elif status_code == 429:
            failure = image_failure(
                "upstream_rate_limited",
                retry_after=retry_after,
                raw_detail=exc.body,
            )
        elif status_code in {408, 504}:
            failure = image_failure(
                "upstream_connection_timeout",
                retry_after=retry_after,
                raw_detail=exc.body,
            )
        elif status_code in {403, 423} or status_code >= 500:
            failure = image_failure(
                "upstream_unavailable",
                retry_after=retry_after,
                raw_detail=exc.body,
            )
        else:
            failure = image_failure(
                "upstream_error",
                retry_after=retry_after,
                raw_detail=exc.body,
            )
        return replace(
            failure,
            status_code=status_code,
            scope="delivery" if credential_scope == "signed_asset" else failure.scope,
            verify_account=False,
        )

    if status_code == 401:
        return image_failure("auth_invalid", retry_after=retry_after, raw_detail=exc.body)
    if (
        structured_failure is not None
        and structured_failure.code == "conversation_not_ready"
        and status_code in {404, 409, 423}
    ):
        return replace(
            image_failure(
                "conversation_not_ready",
                retry_after=retry_after,
                raw_detail=exc.body,
            ),
            status_code=status_code,
        )
    if status_code == 429:
        is_file_upload = (
            context_path in {"/backend-api/files", "image_upload"}
            or (
                context_path.startswith("/backend-api/files/")
                and context_path.endswith("/uploaded")
            )
        )
        if is_file_upload:
            return image_failure("file_upload_throttled", retry_after=retry_after, raw_detail=exc.body)
    if structured_failure is not None and structured_failure.status_code == status_code:
        return structured_failure
    if status_code in {403, 423}:
        return replace(
            image_failure("upstream_unavailable", retry_after=retry_after, raw_detail=exc.body),
            status_code=status_code,
        )
    if status_code == 429:
        return image_failure("upstream_rate_limited", retry_after=retry_after, raw_detail=exc.body)
    if status_code in {408, 504}:
        return image_failure("upstream_connection_timeout", retry_after=retry_after, raw_detail=exc.body)
    if status_code >= 500:
        return replace(
            image_failure("upstream_unavailable", retry_after=retry_after, raw_detail=exc.body),
            status_code=status_code,
        )
    return replace(
        image_failure("upstream_error", retry_after=retry_after, raw_detail=exc.body),
        status_code=status_code,
    )


def classify_oauth_refresh_error(
    status_code: int,
    error_code: str = "",
    raw_detail: Any = None,
) -> ImageFailure:
    status = int(status_code or 0)
    normalized = str(error_code or "").strip().lower()
    aliased = FAILURE_CODE_ALIASES.get(normalized, normalized)
    policy = FAILURE_POLICIES.get(aliased)
    if (
        policy is not None
        and policy.status_code != 400
        and (policy.retryable or policy.verify_account)
    ):
        return image_failure(aliased, raw_detail=raw_detail)
    if status == 401:
        return image_failure("auth_invalid", raw_detail=raw_detail)
    if status == 429:
        return image_failure("upstream_rate_limited", raw_detail=raw_detail)
    if status in {408, 504}:
        return image_failure("upstream_connection_timeout", raw_detail=raw_detail)
    if status in {403, 423} or status >= 500:
        return image_failure("upstream_unavailable", raw_detail=raw_detail)
    if status:
        return image_failure("upstream_error", raw_detail=raw_detail)
    return image_failure("upstream_unavailable", raw_detail=raw_detail)


def classify_image_exception(exc: BaseException, *, code: str | None = None) -> ImageFailure:
    failure = getattr(exc, "failure", None)
    if isinstance(failure, ImageFailure):
        return failure

    def remember(resolved: ImageFailure) -> ImageFailure:
        try:
            setattr(exc, "failure", resolved)
        except (AttributeError, TypeError):
            pass
        return resolved

    if isinstance(exc, UpstreamHTTPError):
        return remember(classify_upstream_http_error(exc))
    if type(exc).__name__ == "OAuthRefreshError":
        return remember(
            classify_oauth_refresh_error(
                int(getattr(exc, "status_code", 0) or 0),
                str(getattr(exc, "error_code", "") or ""),
                str(exc),
            )
        )
    structured_code = code or getattr(exc, "code", None)
    if isinstance(structured_code, str) and structured_code.strip().lower() in (
        FAILURE_POLICIES.keys() | FAILURE_CODE_ALIASES.keys()
    ):
        return remember(image_failure(structured_code, raw_detail=str(exc)))
    if code:
        return remember(image_failure(code, raw_detail=str(exc)))
    if isinstance(exc, (TimeoutError, curl_exceptions.Timeout)):
        return remember(image_failure("upstream_connection_timeout", raw_detail=str(exc)))
    if isinstance(
        exc,
        (
            ConnectionError,
            curl_exceptions.ConnectionError,
            curl_exceptions.ProxyError,
            curl_exceptions.SSLError,
            curl_exceptions.RequestException,
        ),
    ):
        return remember(image_failure("upstream_connection_failed", raw_detail=str(exc)))
    # Unknown exceptions are local by default. Known upstream HTTP, transport,
    # timeout, stream, poll, tool, quota, and auth failures are classified above
    # (or carry a structured ImageFailure) and remain account-attributed.
    return remember(image_failure("internal_error", raw_detail=str(exc)))


def _message(value: Any) -> Mapping[str, Any]:
    item = _mapping(value)
    nested = item.get("message")
    if isinstance(nested, Mapping):
        return nested
    nested_value = _mapping(item.get("v"))
    nested = nested_value.get("message")
    if isinstance(nested, Mapping):
        return nested
    return item


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    content_map = _mapping(content)
    parts = content_map.get("parts")
    if isinstance(parts, list):
        parts_text = "".join(part for part in parts if isinstance(part, str)).strip()
        if parts_text:
            return parts_text
    return str(content_map.get("text") or "").strip()


TERMINAL_MESSAGE_STATUSES = {
    "complete",
    "completed",
    "done",
    "finished",
    "finished_successfully",
    "finished_partial_completion",
    "success",
    "succeeded",
}


def is_terminal_message_status(value: Any) -> bool:
    return str(value or "").strip().lower() in TERMINAL_MESSAGE_STATUSES


def _message_has_image_output(message: Mapping[str, Any]) -> bool:
    author = _mapping(message.get("author"))
    metadata = _mapping(message.get("metadata"))
    if str(author.get("role") or "").strip().lower() != "tool":
        return False
    if str(metadata.get("async_task_type") or "").strip().lower() != "image_gen":
        return False

    def has_pointer(value: Any) -> bool:
        if isinstance(value, Mapping):
            pointer = value.get("asset_pointer")
            if isinstance(pointer, str) and pointer.startswith(("file-service://", "sediment://")):
                return True
            return any(has_pointer(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_pointer(item) for item in value)
        return False

    return has_pointer(message.get("content"))


def classify_upstream_message(value: Any) -> ImageFailure | None:
    outer = _mapping(value)
    event_type = str(outer.get("type") or "").strip().lower()
    if event_type in {"response.failed", "response.incomplete"}:
        response = _mapping(outer.get("response"))
        structured_failure = _classify_structured_failure_codes(
            _structured_codes(outer),
            raw_detail=outer,
        )
        failure = structured_failure or image_failure(
            "image_tool_error",
            raw_detail=outer,
        )
        if event_type == "response.failed":
            failure = failure.with_public_detail(
                response.get("error") or outer.get("error")
            )
        return failure
    moderation = _mapping(outer.get("moderation_response"))
    message = _message(value)
    author = _mapping(message.get("author"))
    metadata = _mapping(message.get("metadata"))
    content = _mapping(message.get("content"))
    role = str(author.get("role") or "").strip().lower()
    content_type = str(content.get("content_type") or "").strip().lower()
    status = str(message.get("status") or metadata.get("status") or "").strip().lower()
    codes = _structured_codes(message)
    raw_detail = _message_text(message)

    return classify_message_facts(
        role=role,
        content_type=content_type,
        status=status,
        end_turn=message.get("end_turn") is True,
        is_error=(
            message.get("is_error") is True
            or metadata.get("is_error") is True
            or bool(message.get("error"))
            or bool(metadata.get("error"))
        ),
        blocked=(
            message.get("blocked") is True
            or metadata.get("blocked") is True
            or (outer.get("type") == "moderation" and moderation.get("blocked") is True)
        ),
        has_image_output=_message_has_image_output(message),
        has_text=bool(raw_detail),
        codes=codes,
        raw_detail=raw_detail or value,
    )


def merge_message_failure(
    current: ImageFailure | None,
    candidate: ImageFailure | None,
) -> ImageFailure | None:
    if candidate is None:
        return current
    if current is None:
        return candidate

    if candidate.code == current.code:
        winner, other = candidate, current
    elif _failure_priority(candidate.code) > _failure_priority(current.code):
        winner, other = candidate, current
    else:
        winner, other = current, candidate
    if not winner.raw_detail and other.raw_detail:
        winner = winner.with_raw_detail(other.raw_detail)
    if not winner.public_detail and other.public_detail:
        winner = winner.with_public_detail(other.public_detail)
    return winner


def classify_task_failure(task: Any) -> ImageFailure | None:
    task_map = _mapping(task)
    image_message = task_map.get("image_gen_message")
    if isinstance(image_message, Mapping):
        failure = classify_upstream_message(image_message)
        if failure is None or failure.code != "image_tool_error":
            return failure

        message = _message(image_message)
        author = _mapping(message.get("author"))
        content = _mapping(message.get("content"))
        role = str(author.get("role") or "").strip().lower()
        content_type = str(content.get("content_type") or "").strip().lower()
        explicit_codes = _structured_codes(message)
        has_explicit_tool_code = any(
            FAILURE_CODE_ALIASES.get(code, code) == "image_tool_error"
            for code in explicit_codes
        )
        if (role == "tool" and content_type == "system_error") or has_explicit_tool_code:
            return failure

        # Task probes often expose only status=failed/is_error. That is real
        # failure evidence, but it is not a specific image-tool classification
        # and must not override a readable terminal assistant response.
        return image_failure("upstream_error", raw_detail=failure.raw_detail)
    return None


def _current_conversation_turn(data: Any) -> list[Mapping[str, Any]]:
    data_map = _mapping(data)
    mapping = _mapping(data_map.get("mapping"))
    messages: list[Mapping[str, Any]] = []
    current_node = str(data_map.get("current_node") or "").strip()

    if current_node and current_node in mapping:
        lineage: list[Mapping[str, Any]] = []
        visited: set[str] = set()
        node_id = current_node
        while node_id and node_id not in visited:
            visited.add(node_id)
            node = _mapping(mapping.get(node_id))
            if not node:
                break
            message = _message(node)
            if message:
                lineage.append(message)
            node_id = str(node.get("parent") or "").strip()
        messages = list(reversed(lineage))
    else:
        ordered: list[tuple[float, str, Mapping[str, Any]]] = []
        for raw_node_id, node in mapping.items():
            message = _message(node)
            if not message:
                continue
            try:
                create_time = float(message.get("create_time") or 0.0)
            except (TypeError, ValueError):
                create_time = 0.0
            ordered.append((create_time, str(raw_node_id), message))
        ordered.sort(key=lambda item: (item[0], item[1]))
        messages = [message for _create_time, _node_id, message in ordered]

    last_user_index = -1
    for message_index, message in enumerate(messages):
        role = str(_mapping(message.get("author")).get("role") or "").strip().lower()
        if role == "user":
            last_user_index = message_index
    return messages[last_user_index + 1:]


def terminal_assistant_text(data: Any) -> str:
    for message in reversed(_current_conversation_turn(data)):
        role = str(_mapping(message.get("author")).get("role") or "").strip().lower()
        content = _mapping(message.get("content"))
        content_type = str(content.get("content_type") or "").strip().lower()
        metadata = _mapping(message.get("metadata"))
        status = str(message.get("status") or metadata.get("status") or "").strip().lower()
        if (
            role == "assistant"
            and content_type in {"text", "code"}
            and (message.get("end_turn") is True or is_terminal_message_status(status))
        ):
            return _message_text(message)
    return ""


def classify_conversation_failure(data: Any) -> ImageFailure | None:
    current_turn = _current_conversation_turn(data)
    if any(_message_has_image_output(message) for message in current_turn):
        return None

    failure: ImageFailure | None = None
    for message in current_turn:
        failure = merge_message_failure(failure, classify_upstream_message(message))
    return failure


def extract_message_facts(value: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {}

    def add_codes(value: Any) -> None:
        candidates = _structured_codes(value)
        if isinstance(value, str) and value.strip():
            candidates.add(value.strip().lower())
        if candidates:
            facts["codes"] = set(facts.get("codes") or ()).union(candidates)

    def record_content(content: Mapping[str, Any]) -> None:
        if content.get("content_type") not in (None, ""):
            facts["content_type"] = str(content.get("content_type") or "").strip().lower()
        parts = content.get("parts")
        if isinstance(parts, list) and any(
            isinstance(part, str) and bool(part.strip())
            for part in parts
        ):
            facts["has_text"] = True
        if isinstance(content.get("text"), str) and str(content.get("text") or "").strip():
            facts["has_text"] = True

    def record_metadata(metadata: Mapping[str, Any]) -> None:
        if metadata.get("is_error") is True or bool(metadata.get("error")):
            facts["is_error"] = True
        if metadata.get("blocked") is True:
            facts["blocked"] = True
        if metadata.get("status") not in (None, ""):
            facts["status"] = str(metadata.get("status") or "").strip().lower()
        for name in ("turn_use_case", "async_task_type", "message_type"):
            if metadata.get(name) not in (None, ""):
                facts[name] = str(metadata.get(name) or "").strip().lower()
        add_codes(metadata)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            message = item.get("message")
            if isinstance(message, Mapping):
                visit(message)
            author = _mapping(item.get("author"))
            content = _mapping(item.get("content"))
            metadata = _mapping(item.get("metadata"))
            if author or content:
                message_id = item.get("id") or item.get("message_id")
                if isinstance(message_id, str) and message_id.strip():
                    facts["message_id"] = message_id.strip()
            if author.get("role") not in (None, ""):
                facts["role"] = str(author.get("role") or "").strip().lower()
            record_content(content)
            record_metadata(metadata)
            if item.get("status") not in (None, ""):
                facts["status"] = str(item.get("status") or "").strip().lower()
            if isinstance(item.get("end_turn"), bool):
                facts["end_turn"] = item["end_turn"]
            if item.get("is_error") is True or bool(item.get("error")):
                facts["is_error"] = True
            if item.get("blocked") is True:
                facts["blocked"] = True
            add_codes({
                key: item.get(key)
                for key in ("code", "error_code", "failure_code")
                if item.get(key) not in (None, "")
            })
            for name in ("turn_use_case", "async_task_type", "message_type"):
                candidate = item.get(name)
                if candidate in (None, ""):
                    candidate = metadata.get(name)
                if candidate not in (None, ""):
                    facts[name] = str(candidate).strip().lower()
            path = str(item.get("p") or "").strip().lower()
            patch_value = item.get("v")
            if path.endswith("/message/metadata") and isinstance(patch_value, Mapping):
                record_metadata(patch_value)
            elif path.endswith("/message/content") and isinstance(patch_value, Mapping):
                record_content(patch_value)
            elif path.endswith("/message/author/role") and isinstance(patch_value, str):
                facts["role"] = patch_value.strip().lower()
            elif path.endswith(("/message/id", "/message/message_id")) and isinstance(patch_value, str):
                facts["message_id"] = patch_value.strip()
            elif path.endswith("/message/content/content_type") and isinstance(patch_value, str):
                facts["content_type"] = patch_value.strip().lower()
            elif path.endswith("/message/content/text") and isinstance(patch_value, str) and patch_value.strip():
                facts["has_text"] = True
            elif path.endswith("/message/content/parts") and isinstance(patch_value, list):
                if any(isinstance(part, str) and part.strip() for part in patch_value):
                    facts["has_text"] = True
            elif "/message/content/parts/" in path and isinstance(patch_value, str) and patch_value.strip():
                facts["has_text"] = True
            elif path.endswith(("/message/status", "/message/metadata/status")) and isinstance(patch_value, str):
                facts["status"] = patch_value.strip().lower()
            elif path.endswith("/message/end_turn") and isinstance(patch_value, bool):
                facts["end_turn"] = patch_value
            elif path.endswith(("/message/is_error", "/message/metadata/is_error")) and patch_value is True:
                facts["is_error"] = True
            elif path.endswith(("/message/error", "/message/metadata/error")) and bool(patch_value):
                facts["is_error"] = True
                add_codes(patch_value)
            elif path.endswith(("/message/blocked", "/message/metadata/blocked")) and patch_value is True:
                facts["blocked"] = True
            elif "/message/" in path and path.rsplit("/", 1)[-1] in {
                "code", "error_code", "failure_code", "type",
            }:
                add_codes(patch_value)
            else:
                for name in ("turn_use_case", "async_task_type", "message_type"):
                    if path.endswith(f"/message/metadata/{name}") and isinstance(patch_value, str):
                        facts[name] = patch_value.strip().lower()
                        break
            for key, child in item.items():
                if key not in {"message", "author", "content", "metadata"} and isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return facts


def classify_message_facts(
    *,
    role: str = "",
    content_type: str = "",
    status: str = "",
    end_turn: bool = False,
    is_error: bool = False,
    blocked: bool = False,
    has_image_output: bool = False,
    has_text: bool = False,
    turn_use_case: str = "",
    async_task_type: str = "",
    message_type: str = "",
    codes: Any = (),
    raw_detail: Any = None,
) -> ImageFailure | None:
    if has_image_output:
        return None

    if blocked:
        return image_failure("content_policy_violation", raw_detail=raw_detail)
    normalized_role = str(role or "").strip().lower()
    normalized_content_type = str(content_type or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    structured_codes = (
        tuple(codes)
        if isinstance(codes, (set, frozenset, list, tuple))
        else (codes,)
    )
    structured_failure = _classify_structured_failure_codes(
        (*structured_codes, normalized_status),
        raw_detail=raw_detail,
    )
    if structured_failure is not None:
        return structured_failure

    if looks_like_conversation_mode_blocked(raw_detail):
        return image_failure(
            "conversation_mode_blocked",
            raw_detail=raw_detail,
        )

    if normalized_role == "assistant" and normalized_content_type == "text" and (
        end_turn or is_terminal_message_status(normalized_status)
    ) and has_text:
        return image_failure(
            "upstream_text_reply",
            raw_detail=raw_detail,
        ).with_public_detail(raw_detail)
    if normalized_role == "tool" and normalized_content_type == "system_error":
        return image_failure("image_tool_error", raw_detail=raw_detail)
    if is_error or normalized_status in FAILED_STATUSES:
        code = "upstream_rate_limited" if is_rate_limit_failure_code(normalized_status) else "upstream_error"
        return image_failure(code, raw_detail=raw_detail)
    return None
