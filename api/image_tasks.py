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

from api.image_task_contract import ImageTaskStatus
from api.image_inputs import image_edit_source_request_hash, parse_image_edit_request, read_image_source_groups
from api.support import (
    allowlisted_trace_headers,
    require_admin,
    require_identity,
    resolve_image_base_url,
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
from services.image_failure import image_failure, public_image_error_message
from services.image_task_view import canonical_image_task_status, image_task_page, image_task_row
from services.log_service import LoggedCall
from services.quota_service import reserve_quota


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


class ImageTaskAssetResponse(BaseModel):
    url: str = ""
    path: str = ""
    b64_json: str = ""
    revised_prompt: str = ""
    width: int | None = None
    height: int | None = None


class ImageTaskResponse(BaseModel):
    id: str
    task_id: str = ""
    client_task_id: str = ""
    status: ImageTaskStatus
    terminal: bool
    mode: str
    model: str
    size: str
    quality: str
    stage_code: str
    stage_label: str
    created_at: str
    updated_at: str
    requested_count: int = Field(ge=1, le=4)
    succeeded_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    error_code: str = ""
    public_error: str = ""
    results: list[ImageTaskAssetResponse] = Field(default_factory=list)
    actions: dict[str, bool] = Field(default_factory=lambda: {"resume_poll": False, "cancel": False})


class ImageTasksResponse(BaseModel):
    items: list[ImageTaskResponse] = Field(default_factory=list)
    missing_ids: list[str] = Field(default_factory=list)
    limit: int = 100
    offset: int = 0


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


def _image_queue_http_exception(exc: Exception) -> HTTPException:
    code = str(getattr(exc, "code", "") or "image_queue_unavailable")
    detail: dict[str, object] = {
        "error": code,
        "message": (
            "image queue storage is full"
            if code == "image_queue_storage_full"
            else "image queue is temporarily unavailable"
        ),
    }
    reason = str(getattr(exc, "reason", "") or "").strip()
    if code == "image_queue_resource_pressure" and reason:
        detail["reason"] = reason[:80]
    return HTTPException(status_code=503, detail=detail)


def _image_stream_error_payload(exc: Exception, task_id: str) -> dict[str, object]:
    if isinstance(exc, _IMAGE_QUEUE_ERRORS):
        detail = _image_queue_http_exception(exc).detail
        if isinstance(detail, dict):
            return {
                "error": {
                    "message": str(detail.get("message") or "image queue is temporarily unavailable"),
                    "type": "server_error",
                    "code": str(detail.get("error") or "image_queue_unavailable"),
                    "task_id": task_id,
                    **(
                        {"reason": str(detail["reason"])[:80]}
                        if detail.get("reason")
                        else {}
                    ),
                },
            }
    failure = image_failure("internal_error")
    return {
        "error": {
            "message": public_image_error_message(failure),
            "type": failure.error_type,
            "code": failure.code,
            "task_id": task_id,
        },
    }


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
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
                "message": (
                    "image task was created but quota state could not be committed; "
                    "poll the task_id or retry with the same idempotency key"
                ),
                "task_id": _task_id_from_result(result),
                "idempotency_key": str(idempotency_key or ""),
            },
        ) from exc


def _http_detail_error(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("error") or detail.get("message") or exc.status_code)
    return str(detail or exc.status_code)


def _log_task_failure(call: LoggedCall, exc: Exception, *, error: str = "") -> None:
    if not error:
        if isinstance(exc, HTTPException):
            error = _http_detail_error(exc)
        else:
            error = str(getattr(exc, "code", "") or str(exc) or exc.__class__.__name__)
    call.log("调用失败", status="failed", error=error)


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
            raise HTTPException(
                status_code=409,
                detail={"error": "image_result_unavailable", "message": str(exc)},
            ) from exc
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
                    base_url=resolve_image_base_url(request),
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
                detail={"error": exc.code, "message": str(exc)},
            ) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            _log_task_failure(call, exc, error=exc.code)
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            _log_task_failure(call, exc)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
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
            raise HTTPException(status_code=400, detail={"error": "client_task_id is required"})
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
                detail={"error": exc.code, "message": str(exc)},
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
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
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
                base_url=resolve_image_base_url(request),
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
                detail={"error": exc.code, "message": str(exc)},
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
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
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
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
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
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "image_result_unavailable",
                    "message": str(exc),
                    "task_id": task_id,
                },
            ) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "image_task_not_found",
                    "message": message,
                    "task_id": task_id,
                },
            ) from exc

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
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

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
                detail={"error": exc.code, "message": str(exc)},
            ) from exc
        except InvalidImageArtifact as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "image_result_unavailable", "message": str(exc)},
            ) from exc
        except _IMAGE_QUEUE_ERRORS as exc:
            raise _image_queue_http_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

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
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        if not snapshot:
            raise HTTPException(status_code=404, detail={"error": "image task not found"})

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
                            pending_payload["public_error"] = (
                                "Image task is still running; continue polling."
                            )
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
                })
                error_payload = _image_stream_error_payload(exc, task_id)
                yield "event: error\n"
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
