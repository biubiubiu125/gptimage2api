from __future__ import annotations

from services.image_failure import ImageFailure, image_failure


EDITABLE_FILE_AUTH_PUBLIC_MESSAGE = "可编辑文件任务无法通过上游账号认证，请更换账号后重试。"
EDITABLE_FILE_REQUEST_PUBLIC_MESSAGE = "可编辑文件请求被上游拒绝。"
EDITABLE_FILE_UNAVAILABLE_PUBLIC_MESSAGE = "可编辑文件服务暂时不可用，请稍后重试。"
EDITABLE_FILE_DOWNLOAD_PUBLIC_MESSAGE = "可编辑文件结果下载失败，请稍后重试。"
EDITABLE_FILE_TRANSFER_PUBLIC_MESSAGE = "可编辑文件资源传输失败，请稍后重试。"
EDITABLE_FILE_TIMEOUT_PUBLIC_MESSAGE = "可编辑文件任务超时，请稍后重试。"
EDITABLE_FILE_ERROR_PUBLIC_MESSAGE = "可编辑文件任务失败，请稍后重试。"
EDITABLE_FILE_INVALID_ID_PUBLIC_MESSAGE = "client_task_id 只能包含字母、数字、点、下划线或短横线，长度 1 到 160。"
EDITABLE_FILE_NOT_FOUND_PUBLIC_MESSAGE = "找不到该可编辑文件任务。"
EDITABLE_FILE_NOT_TERMINAL_PUBLIC_MESSAGE = "任务尚未结束，不能删除。"
EDITABLE_FILE_NO_ACCOUNT_PUBLIC_MESSAGE = "当前没有可用的 Plus/Team/Pro 账号。"
EDITABLE_FILE_EMPTY_IMAGES_PUBLIC_MESSAGE = "必须提供参考图。"
EDITABLE_FILE_INVALID_KIND_PUBLIC_MESSAGE = "可编辑文件类型无效。"
EDITABLE_FILE_INVALID_STORAGE_PUBLIC_MESSAGE = "可编辑文件任务存储无效。"
EDITABLE_FILE_CONFLICT_PUBLIC_MESSAGE = "该可编辑文件任务与已有请求冲突。"
EDITABLE_FILE_CLEANUP_PUBLIC_MESSAGE = "可编辑文件任务文件无法删除。"
EDITABLE_FILE_EXPORT_PUBLIC_MESSAGE = "可编辑文件导出失败，请稍后重试。"
EDITABLE_FILE_CLEANUP_SUFFIX_PUBLIC_MESSAGE = "生成文件未能删除。"

_PUBLIC_BY_ORIGINAL = {
    "client_task_id must be 1-160 characters using letters, numbers, '.', '_' or '-'": (
        EDITABLE_FILE_INVALID_ID_PUBLIC_MESSAGE
    ),
    "editable file task storage id is invalid": EDITABLE_FILE_INVALID_STORAGE_PUBLIC_MESSAGE,
    "no available plus/team/pro account": EDITABLE_FILE_NO_ACCOUNT_PUBLIC_MESSAGE,
    "editable file task not found": EDITABLE_FILE_NOT_FOUND_PUBLIC_MESSAGE,
    "editable file task is not terminal": EDITABLE_FILE_NOT_TERMINAL_PUBLIC_MESSAGE,
    "base64_images is empty": EDITABLE_FILE_EMPTY_IMAGES_PUBLIC_MESSAGE,
    "editable file task kind is invalid": EDITABLE_FILE_INVALID_KIND_PUBLIC_MESSAGE,
    "editable file task output directory is invalid": EDITABLE_FILE_INVALID_STORAGE_PUBLIC_MESSAGE,
    "editable file task staging directory is invalid": EDITABLE_FILE_INVALID_STORAGE_PUBLIC_MESSAGE,
    "editable file export produced invalid artifacts": EDITABLE_FILE_EXPORT_PUBLIC_MESSAGE,
    "editable file output directory already exists": EDITABLE_FILE_EXPORT_PUBLIC_MESSAGE,
    "editable file task files could not be removed": EDITABLE_FILE_CLEANUP_PUBLIC_MESSAGE,
    "editable file task failed": EDITABLE_FILE_ERROR_PUBLIC_MESSAGE,
}
_CLEANUP_FAILED_ORIGINAL_SUFFIX = "; generated files could not be removed"


def _has_public_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def public_editable_exception_message(error: object) -> str:
    public = str(getattr(error, "public_message", "") or "").strip()
    if public:
        return public
    text = str(error or "").strip()
    if text.endswith(_CLEANUP_FAILED_ORIGINAL_SUFFIX):
        base = text[: -len(_CLEANUP_FAILED_ORIGINAL_SUFFIX)].strip()
        mapped = public_editable_exception_message(base)
        if mapped:
            return f"{mapped} {EDITABLE_FILE_CLEANUP_SUFFIX_PUBLIC_MESSAGE}"
    mapped = _PUBLIC_BY_ORIGINAL.get(text)
    if mapped:
        return mapped
    if _has_public_chinese(text):
        return text
    return EDITABLE_FILE_ERROR_PUBLIC_MESSAGE


def public_editable_file_error_message(failure: ImageFailure) -> str:
    """Project a shared upstream failure into the Editable File domain."""
    if failure.code == "auth_invalid":
        return EDITABLE_FILE_AUTH_PUBLIC_MESSAGE
    if failure.code in {
        "content_policy_violation",
        "invalid_image_input",
        "upstream_text_reply",
        "unsupported_model",
    }:
        return EDITABLE_FILE_REQUEST_PUBLIC_MESSAGE
    if failure.code in {
        "file_upload_throttled",
        "image_quota_exhausted",
        "insufficient_quota",
        "upstream_rate_limited",
        "upstream_unavailable",
    }:
        return EDITABLE_FILE_UNAVAILABLE_PUBLIC_MESSAGE
    if failure.code == "image_download_failed":
        return EDITABLE_FILE_DOWNLOAD_PUBLIC_MESSAGE
    if failure.scope == "delivery":
        return EDITABLE_FILE_TRANSFER_PUBLIC_MESSAGE
    if failure.code in {
        "image_poll_timeout",
        "image_stream_timeout",
        "upstream_connection_timeout",
    }:
        return EDITABLE_FILE_TIMEOUT_PUBLIC_MESSAGE
    return EDITABLE_FILE_ERROR_PUBLIC_MESSAGE


class EditableFileFailureError(RuntimeError):
    """Structured Editable File failure with a domain-specific public message."""

    def __init__(self, *, failure: ImageFailure | None = None) -> None:
        self.failure = failure or image_failure("upstream_error")
        self.status_code = self.failure.status_code
        self.error_type = self.failure.error_type
        super().__init__(public_editable_file_error_message(self.failure))
