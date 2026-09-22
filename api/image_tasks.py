from __future__ import annotations

import asyncio
import logging
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.image_task_contract import ImageTaskPage, ImageTaskRow
from api.image_inputs import image_edit_source_request_hash, parse_image_edit_request, read_image_source_groups
from api.support import (
    allowlisted_trace_headers,
    require_admin,
    require_identity,
    resolve_optional_image_base_url,
)
from services.content_filter import check_request
from services.account_service import account_service
from services.config import config
from services.image_task_service import image_task_service
from services.image_queue.artifact_service import InvalidImageArtifact
from services.image_queue.database import ImageQueueUnavailableError
from services.image_queue.idempotency import select_idempotency_key
from services.image_queue.repository import IdempotencyConflict, TaskStateConflict
from services.image_queue.resource_controller import (
    ImageQueueResourcePressureError,
    ImageQueueStorageFullError,
)
from services.image_failure import (
    IMAGE_INPUT_INVALID_PUBLIC_MESSAGE,
    IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE,
    IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE,
    IMAGE_TASK_PENDING_PUBLIC_MESSAGE,
    QUOTA_COMMIT_FAILED_PUBLIC_MESSAGE,
    ImageGenerationError,
    image_failure,
    image_queue_http_message,
    public_http_chinese_error,
    public_image_error_message,
)
from services.protocol.error_response import VALIDATION_ERROR_PUBLIC_MESSAGE
from services.image_task_view import canonical_image_task_status, image_task_page, image_task_row
from services.log_service import (
    LoggedCall,
    http_exception_admin_message,
    http_exception_log_fields,
)
from services.quota_service import image_quota_units, reserve_quota


logger = logging.getLogger(__name__)

_IMAGE_QUEUE_ERRORS = (
    ImageQueueUnavailableError,
    ImageQueueResourcePressureError,
    ImageQueueStorageFullError,
)


class ImageGenerationTaskRequest(BaseModel):
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"


class ResumePollRequest(BaseModel):
    extra_timeout_secs: float = Field(
        default_factory=lambda: float(config.image_poll_timeout_secs),
        ge=5.0,
        le=120.0,
    )


ImageTaskResponse = ImageTaskRow
ImageTasksResponse = ImageTaskPage


class ImageQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_quota: int
    unlimited_quota_count: int
    unknown_quota_count: int
    active_accounts: int
    limited_accounts: int
    abnormal_accounts: int
    disabled_accounts: int
    available: bool


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _public_task_row(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("image task snapshot must be an object")
    return image_task_row(value)


def _public_task_page(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("image task page must be an object")
    projected = image_task_page(
        value.get("items") if isinstance(value.get("items"), list) else [],
        missing_ids=value.get("missing_ids") if isinstance(value.get("missing_ids"), list) else [],
    )
    projected["limit"] = value.get("limit", 100)
    projected["offset"] = value.get("offset", 0)
    return projected


def _image_quota_payload(stats: dict) -> dict[str, object]:
    total_quota = max(0, int(stats.get("total_quota") or 0))
    unlimited = max(0, int(stats.get("unlimited_quota_count") or 0))
    unknown = max(0, int(stats.get("unknown_quota_count") or 0))
    return {
        "total_quota": total_quota,
        "unlimited_quota_count": unlimited,
        "unknown_quota_count": unknown,
        "active_accounts": max(0, int(stats.get("active") or 0)),
        "limited_accounts": max(0, int(stats.get("limited") or 0)),
        "abnormal_accounts": max(0, int(stats.get("abnormal") or 0)),
        "disabled_accounts": max(0, int(stats.get("disabled") or 0)),
        "available": total_quota > 0 or unlimited > 0 or unknown > 0,
    }


def _http_stage_for_code(code: str, default: str = "请求参数") -> str:
    return {
        "unsupported_model": "模型不支持",
        "idempotency_conflict": "幂等冲突",
        "task_state_conflict": "任务状态",
        "invalid_image_input": "参考图无效",
        "image_task_not_found": "任务不存在",
        "quota_commit_failed": "额度提交",
        "image_queue_unavailable": "队列不可用",
        "image_queue_resource_pressure": "队列不可用",
        "image_queue_storage_full": "存储已满",
        "image_result_unavailable": "结果不可用",
        "invalid_client_task_id": "请求参数",
        "idempotency_key_required": "请求参数",
        "editable_file_task_not_found": "可编辑文件",
        "editable_file_conflict": "可编辑文件",
        "editable_file_task_not_terminal": "可编辑文件",
    }.get(str(code or "").strip(), default)


def _image_queue_http_exception(exc: Exception) -> HTTPException:
    code = str(getattr(exc, "code", "") or "image_queue_unavailable")
    return HTTPException(
        status_code=503,
        detail={
            "error": code,
            "message": image_queue_http_message(code),
        },
    )


def _has_public_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _public_value_error_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, InvalidImageArtifact):
        return HTTPException(
            status_code=400,
            detail={
                "error": "invalid_image_input",
                "message": public_http_chinese_error(
                    stage="参考图无效",
                    reason=IMAGE_INPUT_INVALID_PUBLIC_MESSAGE,
                ),
            },
        )
    public = str(getattr(exc, "public_message", "") or "").strip()
    code = str(getattr(exc, "code", "") or "").strip() or "bad_request"
    if public:
        return HTTPException(
            status_code=400,
            detail={
                "error": code,
                "message": public_http_chinese_error(
                    stage=_http_stage_for_code(code),
                    reason=public,
                ),
            },
        )
    text = str(exc).strip()
    if _has_public_chinese(text):
        return HTTPException(
            status_code=400,
            detail={
                "error": public_http_chinese_error(stage="请求参数", reason=text),
            },
        )
    return HTTPException(
        status_code=400,
        detail={
            "error": "bad_request",
            "message": public_http_chinese_error(
                stage="请求参数",
                reason=VALIDATION_ERROR_PUBLIC_MESSAGE,
            ),
        },
    )


def _image_task_not_found_http_exception(task_id: str = "") -> HTTPException:
    detail: dict[str, object] = {
        "error": "image_task_not_found",
        "message": public_http_chinese_error(
            stage="任务不存在",
            reason=IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE,
        ),
    }
    if task_id:
        detail["task_id"] = task_id
    return HTTPException(status_code=404, detail=detail)


def _image_result_unavailable_http_exception(
    exc: Exception | None = None,
    *,
    task_id: str = "",
) -> HTTPException:
    detail: dict[str, object] = {
        "error": "image_result_unavailable",
        "message": public_http_chinese_error(
            stage="结果不可用",
            reason=IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE,
        ),
    }
    if task_id:
        detail["task_id"] = task_id
    http_exc = HTTPException(status_code=409, detail=detail)
    if isinstance(exc, BaseException):
        http_exc.__cause__ = exc
    return http_exc


def _stream_openai_error(
    *,
    message: str,
    code: str,
    error_type: str,
    task_id: str,
) -> dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
            "task_id": task_id,
        },
    }


def _stream_error_from_http_detail(detail: object, task_id: str) -> dict[str, object] | None:
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, dict):
            message = str(nested.get("message") or "").strip()
            code = str(nested.get("code") or nested.get("type") or "internal_error").strip()
            error_type = str(nested.get("type") or "server_error").strip() or "server_error"
        else:
            message = str(detail.get("message") or "").strip()
            code = str(detail.get("error") or "internal_error").strip() or "internal_error"
            error_type = "server_error"
        if message:
            return _stream_openai_error(
                message=message,
                code=code,
                error_type=error_type,
                task_id=task_id,
            )
        return None
    text = str(detail or "").strip()
    if not text:
        return None
    return _stream_openai_error(
        message=text,
        code="internal_error",
        error_type="server_error",
        task_id=task_id,
    )


def _image_stream_error_payload(exc: Exception, task_id: str) -> dict[str, object]:
    if isinstance(exc, _IMAGE_QUEUE_ERRORS):
        payload = _stream_error_from_http_detail(
            _image_queue_http_exception(exc).detail,
            task_id,
        )
        if payload is not None:
            return payload
    if isinstance(exc, HTTPException):
        payload = _stream_error_from_http_detail(exc.detail, task_id)
        if payload is not None:
            return payload
    if isinstance(exc, ImageGenerationError):
        payload = exc.to_openai_error()
        error = payload.get("error")
        if isinstance(error, dict):
            error["task_id"] = task_id or str(error.get("task_id") or "")
        return payload
    if isinstance(exc, InvalidImageArtifact):
        failure = image_failure("invalid_image_result")
        return _stream_openai_error(
            message=public_image_error_message(failure),
            code=failure.code,
            error_type=failure.error_type,
            task_id=task_id,
        )
    if isinstance(exc, TimeoutError):
        failure = image_failure("image_task_pending")
        return _stream_openai_error(
            message=public_image_error_message(failure),
            code=failure.code,
            error_type=failure.error_type,
            task_id=task_id,
        )
    if str(exc or "").strip() == IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE:
        payload = _stream_error_from_http_detail(
            _image_task_not_found_http_exception(task_id).detail,
            task_id,
        )
        if payload is not None:
            return payload
    failure = image_failure("internal_error")
    return _stream_openai_error(
        message=public_image_error_message(failure),
        code=failure.code,
        error_type=failure.error_type,
        task_id=task_id,
    )


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        _log_task_failure(call, exc)
        raise


def _task_id_from_result(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("task_id") or "").strip()
    return ""


def _commit_quota_or_raise(quota, result: object, idempotency_key: str) -> None:
    try:
        quota.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "quota_commit_failed",
                "message": public_http_chinese_error(
                    stage="额度提交",
                    reason=QUOTA_COMMIT_FAILED_PUBLIC_MESSAGE,
                ),
                "task_id": _task_id_from_result(result),
                "idempotency_key": str(idempotency_key or ""),
            },
        ) from exc


def _admin_task_failure_message(exc: Exception, *, error: str = "") -> str:
    original = str(exc or "").strip() or exc.__class__.__name__
    code = str(getattr(exc, "code", "") or "").strip()
    requested = str(error or "").strip()
    if requested and requested not in {code, original}:
        return requested
    return original


def _public_task_failure_message(exc: Exception) -> str:
    public = str(getattr(exc, "public_message", "") or "").strip()
    if public:
        return public
    if isinstance(exc, _IMAGE_QUEUE_ERRORS):
        return image_queue_http_message(str(getattr(exc, "code", "") or ""))
    return ""


def _log_task_failure(call: LoggedCall, exc: Exception, *, error: str = "") -> None:
    extra = None
    if isinstance(exc, HTTPException):
        extra = http_exception_log_fields(exc)
        admin = error or http_exception_admin_message(exc)
    else:
        admin = _admin_task_failure_message(exc, error=error)
        extra = {
            "raw_error": admin,
            "upstream_error": admin,
            "raw_detail": admin,
        }
        public = _public_task_failure_message(exc)
        if public:
            extra["public_error"] = public
        code = str(getattr(exc, "code", "") or "").strip()
        if code:
            extra["error_code"] = code
    call.log("调用失败", status="failed", error=admin, extra=extra)


def _log_task_queued(call: LoggedCall, result: object) -> None:
    task_id = _task_id_from_result(result)
    extra = {"task_id": task_id} if task_id else None
    call.log("已入队", result, status="queued", extra=extra)


async def _finalize_task_call_record(call: LoggedCall, result: object) -> None:
    task_id = _task_id_from_result(result)
    finalizer = getattr(image_task_service, "finalize_call_record", None)
    if not task_id or not callable(finalizer):
        return
    await run_in_threadpool(finalizer, task_id, call_id=call.call_id)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks", response_model=ImageTasksResponse)
    async def list_image_tasks(
        ids: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            result = await run_in_threadpool(
                image_task_service.list_tasks,
                identity,
                _parse_task_ids(ids),
                limit,
                offset,
            )
            return _public_task_page(result)
        except InvalidImageArtifact as exc:
            raise _image_result_unavailable_http_exception(exc) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc

    @router.get("/api/image-tasks/quota", response_model=ImageQuotaResponse)
    async def image_quota_summary(
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        stats = await run_in_threadpool(account_service.get_stats)
        return _image_quota_payload(stats)

    @router.post("/api/image-tasks/generations", response_model=ImageTaskResponse)
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/api/image-tasks/generations", body.model, "文生图任务", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        trace_headers = allowlisted_trace_headers(request.headers)
        trace_headers["call_id"] = call.call_id
        try:
            idempotency_key = select_idempotency_key(request.headers, body.client_task_id)
            quota = reserve_quota(
                identity,
                "/api/image-tasks/generations",
                body.model,
                image_request=True,
                idempotency_key=idempotency_key,
                idempotency_aliases=[body.client_task_id],
                units=body.n,
            )
            try:
                result = await run_in_threadpool(
                    image_task_service.submit_generation,
                    identity,
                    client_task_id=body.client_task_id,
                    prompt=body.prompt,
                    model=body.model,
                    n=body.n,
                    size=body.size,
                    quality=body.quality,
                    base_url=resolve_optional_image_base_url(request),
                    idempotency_key=idempotency_key,
                    trace_headers=trace_headers,
                    source_endpoint="/api/image-tasks/generations",
                    request_started_at=call.started,
                )
            except Exception:
                quota.cancel()
                raise
            _commit_quota_or_raise(quota, result, idempotency_key)
            _log_task_queued(call, result)
            await _finalize_task_call_record(call, result)
            return _public_task_row(result)
        except HTTPException as exc:
            _log_task_failure(call, exc)
            raise
        except IdempotencyConflict as exc:
            _log_task_failure(call, exc, error=exc.code)
            raise HTTPException(
                status_code=409,
                detail={"error": exc.code, "message": exc.public_message},
            ) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            _log_task_failure(call, exc, error=exc.code)
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            _log_task_failure(call, exc)
            raise _public_value_error_http_exception(exc) from exc
        except Exception as exc:
            _log_task_failure(call, exc)
            raise

    @router.post("/api/image-tasks/edits", response_model=ImageTaskResponse)
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        client_task_id = str(payload.get("client_task_id") or "").strip()
        if not client_task_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_client_task_id",
                    "message": public_http_chinese_error(
                        stage="请求参数",
                        reason="必须提供 client_task_id。",
                    ),
                },
            )
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        call = LoggedCall(identity, "/api/image-tasks/edits", model, "图生图任务", request_text=prompt)
        await filter_or_log(call, prompt)
        trace_headers = allowlisted_trace_headers(request.headers)
        trace_headers["call_id"] = call.call_id
        quota = None
        existing = None
        try:
            idempotency_key = select_idempotency_key(request.headers, client_task_id)
            quota = reserve_quota(
                identity,
                "/api/image-tasks/edits",
                model,
                image_request=True,
                idempotency_key=idempotency_key,
                idempotency_aliases=[client_task_id],
                units=image_quota_units(payload.get("n")),
            )
            source_request_hash = image_edit_source_request_hash(payload, image_sources, mask_sources)
            existing = await run_in_threadpool(
                image_task_service.replay_existing_edit_task,
                identity,
                client_task_id=client_task_id,
                idempotency_key=idempotency_key,
                source_request_hash=source_request_hash,
            )
        except IdempotencyConflict as exc:
            if quota is not None:
                quota.cancel()
            _log_task_failure(call, exc, error=exc.code)
            raise HTTPException(
                status_code=409,
                detail={"error": exc.code, "message": exc.public_message},
            ) from exc
        except HTTPException as exc:
            if quota is not None:
                quota.cancel()
            _log_task_failure(call, exc)
            raise
        except _IMAGE_QUEUE_ERRORS as exc:
            if quota is not None:
                quota.cancel()
            _log_task_failure(call, exc, error=exc.code)
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            if quota is not None:
                quota.cancel()
            _log_task_failure(call, exc)
            raise _public_value_error_http_exception(exc) from exc
        except Exception as exc:
            if quota is not None:
                quota.cancel()
            _log_task_failure(call, exc)
            raise
        if existing is not None:
            try:
                _commit_quota_or_raise(quota, existing, idempotency_key)
            except HTTPException as exc:
                _log_task_failure(call, exc)
                raise
            _log_task_queued(call, existing)
            await _finalize_task_call_record(call, existing)
            return _public_task_row(existing)
        try:
            images, resolved_masks = await read_image_source_groups(image_sources, mask_sources)
            masks = resolved_masks or None
            result = await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=model,
                n=payload.get("n", 1),
                size=payload["size"],
                quality=payload["quality"],
                base_url=resolve_optional_image_base_url(request),
                images=images,
                masks=masks,
                idempotency_key=idempotency_key,
                trace_headers=trace_headers,
                response_format=payload["response_format"],
                source_request_hash=source_request_hash,
                source_endpoint="/api/image-tasks/edits",
                request_started_at=call.started,
            )
        except IdempotencyConflict as exc:
            quota.cancel()
            _log_task_failure(call, exc, error=exc.code)
            raise HTTPException(
                status_code=409,
                detail={"error": exc.code, "message": exc.public_message},
            ) from exc
        except HTTPException as exc:
            quota.cancel()
            _log_task_failure(call, exc)
            raise
        except _IMAGE_QUEUE_ERRORS as exc:
            quota.cancel()
            _log_task_failure(call, exc, error=exc.code)
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            quota.cancel()
            _log_task_failure(call, exc)
            raise _public_value_error_http_exception(exc) from exc
        except Exception as exc:
            quota.cancel()
            _log_task_failure(call, exc)
            raise
        try:
            _commit_quota_or_raise(quota, result, idempotency_key)
        except HTTPException as exc:
            _log_task_failure(call, exc)
            raise
        _log_task_queued(call, result)
        await _finalize_task_call_record(call, result)
        return _public_task_row(result)

    @router.post("/api/image-tasks/{task_id}/resume-poll", response_model=ImageTaskResponse)
    async def resume_image_poll(
        task_id: str,
        body: ResumePollRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            result = await run_in_threadpool(
                image_task_service.resume_poll,
                identity,
                task_id,
                body.extra_timeout_secs,
            )
            return _public_task_row(result)
        except ValueError as exc:
            if str(exc).strip() == IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE:
                raise _image_task_not_found_http_exception(task_id) from exc
            raise _public_value_error_http_exception(exc) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc

    @router.get("/api/image-tasks/{task_id}", response_model=ImageTaskResponse)
    async def get_image_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            result = await run_in_threadpool(image_task_service.get_task, identity, task_id)
            return _public_task_row(result)
        except InvalidImageArtifact as exc:
            raise _image_result_unavailable_http_exception(exc, task_id=task_id) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            raise _image_task_not_found_http_exception(task_id) from exc

    @router.post("/api/image-tasks/{task_id}/cancel", response_model=ImageTaskResponse)
    async def cancel_image_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            result = await run_in_threadpool(image_task_service.cancel, identity, task_id)
            return _public_task_row(result)
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            raise _image_task_not_found_http_exception(task_id) from exc

    @router.post("/api/image-tasks/{task_id}/ack", response_model=ImageTaskResponse)
    async def acknowledge_image_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            result = await run_in_threadpool(image_task_service.acknowledge, identity, task_id)
            return _public_task_row(result)
        except TaskStateConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": exc.code, "message": exc.public_message},
            ) from exc
        except InvalidImageArtifact as exc:
            raise _image_result_unavailable_http_exception(exc, task_id=task_id) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            raise _image_task_not_found_http_exception(task_id) from exc

    @router.get("/api/image-tasks/{task_id}/stream")
    async def stream_image_task(
        task_id: str,
        authorization: str | None = Header(default=None),
        timeout: float = Query(default=300.0, ge=10.0, le=600.0),
    ):
        """订阅已有任务的 SSE 流，复用 wait_for_terminal_async 的通知机制。

        适用场景：客户端已通过 POST /api/image-tasks/generations 拿到 task_id，
        之后用此端点订阅而非轮询。事件词汇与 /v1/images/* 流式响应一致，
        但 payload 是 ImageTaskResponse 形状而非 OpenAI image chunk。
        """
        identity = require_identity(authorization)

        # 提前校验任务是否存在且归属正确，让 404 走 JSON 响应而非流内错误。
        try:
            snapshot = await run_in_threadpool(
                image_task_service.progress_snapshot,
                identity,
                task_id,
            )
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            raise _image_task_not_found_http_exception(task_id) from exc
        if not snapshot:
            raise _image_task_not_found_http_exception(task_id)

        mode = str(snapshot.get("mode") or "generate")
        event_prefix = "image_edit" if mode == "edit" else "image_generation"
        terminal_statuses = {"succeeded", "partial_success", "failed", "cancelled"}
        completed_statuses = {"succeeded", "partial_success"}

        def _public_status(value: object) -> str:
            if not isinstance(value, dict):
                return "failed"
            data = value.get("data")
            result_count = len(data) if isinstance(data, list) else 0
            try:
                requested_count = int(value.get("required_jobs") or value.get("n") or 0)
            except (TypeError, ValueError):
                requested_count = 0
            if requested_count <= 0:
                try:
                    requested_count = int(value.get("succeeded_jobs") or 0) + int(
                        value.get("failed_jobs") or 0
                    )
                except (TypeError, ValueError):
                    requested_count = 0
            requested_count = max(1, requested_count)
            return canonical_image_task_status(
                value.get("status"),
                result_count=result_count,
                requested_count=requested_count,
            )

        def _snapshot_json(value: object) -> str:
            return json.dumps(_public_task_row(value), ensure_ascii=False)

        async def event_generator() -> AsyncIterator[str]:
            current_snapshot = snapshot
            task_status = _public_status(current_snapshot)
            already_terminal = task_status in terminal_statuses

            # 首事件：初始快照。
            if already_terminal:
                try:
                    current_snapshot = await run_in_threadpool(
                        image_task_service.get_task,
                        identity,
                        task_id,
                    )
                except Exception as exc:
                    logger.warning({
                        "event": "image_task_stream_terminal_refresh_failed",
                        "task_id": task_id,
                        "error_type": exc.__class__.__name__,
                    })
                    error_payload = _image_stream_error_payload(exc, task_id)
                    yield "event: error\n"
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                task_status = _public_status(current_snapshot)
                already_terminal = task_status in terminal_statuses
                if not already_terminal:
                    event_type = "queued" if task_status == "pending" else "in_progress"
                    yield f"event: {event_prefix}.{event_type}\n"
                    yield f"data: {_snapshot_json(current_snapshot)}\n\n"
                else:
                    status = "completed" if task_status in completed_statuses else "failed"
                    yield f"event: {event_prefix}.{status}\n"
                    yield f"data: {_snapshot_json(current_snapshot)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
            else:
                event_type = "queued" if task_status == "pending" else "in_progress"
                yield f"event: {event_prefix}.{event_type}\n"
                yield f"data: {_snapshot_json(current_snapshot)}\n\n"

            # 心跳 + 终态轮询。
            deadline = time.monotonic() + timeout
            heartbeat_interval = 15.0
            try:
                while True:
                    remaining = max(0.1, deadline - time.monotonic())
                    poll_timeout = min(heartbeat_interval, remaining)
                    try:
                        terminal = await asyncio.wait_for(
                            image_task_service.wait_for_terminal_async(identity, task_id, poll_timeout),
                            timeout=poll_timeout + 1.0,
                        )
                        # 拿到终态快照。
                        status = _public_status(terminal)
                        event_type = "completed" if status in completed_statuses else "failed"
                        yield f"event: {event_prefix}.{event_type}\n"
                        yield f"data: {_snapshot_json(terminal)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    except asyncio.TimeoutError:
                        # 心跳：拉取当前进度。
                        progress = await run_in_threadpool(
                            image_task_service.progress_snapshot,
                            identity,
                            task_id,
                        )
                        if progress:
                            yield f"event: {event_prefix}.in_progress\n"
                            yield f"data: {_snapshot_json(progress)}\n\n"
                        if time.monotonic() >= deadline:
                            # 订阅超时不等于任务失败：保留非终态，并提示客户端继续轮询。
                            pending_payload = _public_task_row(progress or current_snapshot)
                            pending_payload["error_code"] = "image_task_pending"
                            pending_payload["public_error"] = IMAGE_TASK_PENDING_PUBLIC_MESSAGE
                            yield f"event: {event_prefix}.in_progress\n"
                            yield f"data: {json.dumps(pending_payload, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
            except Exception as exc:
                # 流内异常：发 error 事件 + [DONE]。
                logger.warning({
                    "event": "image_task_stream_error",
                    "task_id": task_id,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                })
                error_payload = _image_stream_error_payload(exc, task_id)
                yield "event: error\n"
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
