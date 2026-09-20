from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from services.image_failure import (
    IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE,
    IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE,
    IMAGE_TASK_PENDING_PUBLIC_MESSAGE,
    ImageGenerationError,
    classify_image_exception,
    image_failure,
    is_formatted_public_chinese_error,
    public_error_original,
    public_image_error_message,
)
from services.image_queue.artifact_service import InvalidImageArtifact
from services.image_delivery import is_url_only_result
from services.image_task_service import image_task_service
from services.protocol.conversation import ImageOutput


_PREPARED_RESULT_KEY = "_durable_image_prepared_result"
_SUBMISSION_KEY = "_durable_image_submission"
_ALLOWED_RESPONSE_FORMATS = {"b64_json", "url"}


def normalize_response_format(value: object, default: str = "b64_json") -> str:
    response_format = str(value or default).strip() or default
    if response_format not in _ALLOWED_RESPONSE_FORMATS:
        raise ImageGenerationError(
            "response_format 只支持 b64_json 或 url。",
            failure=image_failure(
                "invalid_image_input",
                raw_detail=f"不支持的 response_format：{response_format}",
            ),
        )
    return response_format


def _resolve_response_format(
    submission: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    fallback: str,
) -> str:
    return normalize_response_format(
        submission.get("response_format")
        or request_payload.get("response_format")
        or fallback,
        "b64_json",
    )


def _protocol_wait_timeout() -> float:
    settings = getattr(image_task_service, "settings", None)
    return float(getattr(settings, "protocol_wait_timeout_seconds", 300) or 300)


def _task_error(exc: Exception, task_id: str) -> ImageGenerationError:
    resolved_task_id = str(task_id or "").strip()
    if isinstance(exc, ImageGenerationError):
        if not exc.task_id and resolved_task_id:
            exc.task_id = resolved_task_id
        return exc
    if isinstance(exc, TimeoutError):
        return ImageGenerationError(
            IMAGE_TASK_PENDING_PUBLIC_MESSAGE,
            failure=image_failure(
                "image_task_pending",
                raw_detail=IMAGE_TASK_PENDING_PUBLIC_MESSAGE,
            ),
            task_id=resolved_task_id,
        )
    if isinstance(exc, InvalidImageArtifact):
        return _unavailable_image_result_error(str(exc or "").strip(), resolved_task_id)
    if str(exc or "").strip() == IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE:
        failure = image_failure("image_task_not_found")
        return ImageGenerationError(
            IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE,
            failure=failure,
            raw_error=IMAGE_TASK_NOT_FOUND_PUBLIC_MESSAGE,
            task_id=resolved_task_id,
        )
    failure = classify_image_exception(exc)
    message = str(exc or "图片任务失败。")
    return ImageGenerationError(message, failure=failure, task_id=resolved_task_id)


def has_durable_context(body: Mapping[str, Any]) -> bool:
    return isinstance(body.get("_image_task_context"), dict)


def _context(body: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, object]]:
    context = body.get("_image_task_context")
    if not isinstance(context, dict):
        raise ValueError("图片生成必须走持久化图片队列。")
    identity = context.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("图片任务身份信息无效。")
    return context, identity


def _submit(
    body: Mapping[str, Any],
    payload: Mapping[str, Any],
    mode: str,
    response_format: str,
) -> tuple[dict[str, Any], dict[str, object], dict[str, Any], str]:
    context, identity = _context(body)
    request_payload = dict(payload)
    request_payload["base_url"] = str(context.get("base_url") or request_payload.get("base_url") or "")
    request_payload["response_format"] = response_format
    source_endpoint = str(context.get("source_endpoint") or "").strip()
    if source_endpoint:
        request_payload["source_endpoint"] = source_endpoint[:255]
    if context.get("request_started_at"):
        request_payload["request_started_at"] = context.get("request_started_at")
    trace_headers = dict(context.get("trace_headers") or {}) if isinstance(context.get("trace_headers"), dict) else {}
    call_id = str(body.get("_call_id") or "").strip()
    if call_id:
        trace_headers["call_id"] = call_id
    submitted = image_task_service.submit_protocol_request(
        identity,
        request_payload,
        mode,
        str(context.get("idempotency_key") or ""),
        trace_headers,
    )
    return context, identity, request_payload, str(submitted.get("task_id") or "")


def _submission(
    body: Mapping[str, Any],
    payload: Mapping[str, Any],
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    response_format = normalize_response_format(response_format, "b64_json")
    existing = body.get(_SUBMISSION_KEY)
    if isinstance(existing, dict) and str(existing.get("task_id") or ""):
        return existing
    _context_value, identity, request_payload, task_id = _submit(
        body,
        payload,
        mode,
        response_format,
    )
    if not task_id:
        raise RuntimeError("提交图片任务后没有返回任务 ID。")
    submitted = {
        "identity": identity,
        "request_payload": request_payload,
        "task_id": task_id,
        "response_format": response_format,
    }
    if isinstance(body, dict):
        body[_SUBMISSION_KEY] = submitted
    return submitted


def _raise_after_response_attempt(identity: Mapping[str, object], task_id: str, error: ImageGenerationError) -> None:
    image_task_service.mark_response_attempted(identity, task_id)
    raise error


def _unavailable_image_result_error(original: str, task_id: str = "") -> ImageGenerationError:
    detail = str(original or "").strip()
    return ImageGenerationError(
        public_image_error_message(image_failure("invalid_image_result")),
        failure=image_failure("invalid_image_result", raw_detail=detail),
        raw_error=detail,
        task_id=task_id,
    )


def _invalid_image_result_error(
    identity: Mapping[str, object],
    task_id: str,
    reason: str,
    *,
    item: Mapping[str, Any] | None = None,
    extra: str = "",
) -> None:
    parts = [reason]
    if extra:
        parts.append(extra)
    if isinstance(item, Mapping):
        for key in ("relative_path", "url", "width", "height", "checksum", "sha256"):
            value = item.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
    original = " ".join(str(part) for part in parts if str(part).strip())
    _raise_after_response_attempt(
        identity,
        task_id,
        _unavailable_image_result_error(original, task_id),
    )


async def prepare_submission(
    body: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _submission,
        body,
        payload,
        mode,
        response_format,
    )


async def prepare_request(
    body: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    if body.get("stream"):
        return await prepare_submission(
            body,
            payload,
            mode=mode,
            response_format=response_format,
        )
    return await prepare(
        body,
        payload,
        mode=mode,
        response_format=response_format,
    )


def _result(
    identity: Mapping[str, object],
    task_id: str,
    terminal: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    response_format: str,
) -> dict[str, Any]:
    status = str(terminal.get("status") or "")
    required = int(terminal.get("required_jobs") or 0)
    succeeded = int(terminal.get("succeeded_jobs") or 0)
    error_code = str(terminal.get("error_code") or "").strip()
    public_error = str(terminal.get("public_error") or "").strip()
    legacy_error = str(terminal.get("error") or terminal.get("error_message") or "").strip()
    if is_formatted_public_chinese_error(public_error):
        error_message = public_error
    elif is_formatted_public_chinese_error(legacy_error):
        error_message = legacy_error
    else:
        error_message = legacy_error or public_error or "图片生成失败。"
    admin_error = str(terminal.get("_admin_error") or "").strip() or public_error_original(error_message) or error_message
    # The task service returns the public projection here. A terminal task
    # with retained results is exposed as "partial_success" when some
    # requested jobs failed.
    is_partial = status == "partial_success" or (
        status in {"success", "succeeded"} and 0 < succeeded < required
    )
    is_success = status in {"success", "succeeded", "partial_success"}
    if not is_success:
        image_task_service.mark_response_attempted(identity, task_id)
        raise ImageGenerationError(
            error_message,
            failure=image_failure(error_code, raw_detail=admin_error),
            raw_error=admin_error,
            task_id=task_id,
        )
    if (
        required <= 0
        or succeeded <= 0
        or succeeded > required
        or (not is_partial and succeeded < required)
        or (is_partial and succeeded >= required)
    ):
        image_task_service.mark_response_attempted(identity, task_id)
        raise ImageGenerationError(
            error_message,
            failure=image_failure(error_code or "image_job_failed", raw_detail=admin_error),
            raw_error=admin_error,
            task_id=task_id,
        )

    data: list[dict[str, Any]] = []
    image_urls: list[str] = []
    legacy_import = bool(request_payload.get("legacy_import") or terminal.get("legacy_import"))
    for raw_item in terminal.get("data") or []:
        if not isinstance(raw_item, dict):
            continue
        width = int(raw_item.get("width") or 0)
        height = int(raw_item.get("height") or 0)
        if width <= 0 or height <= 0:
            _invalid_image_result_error(
                identity,
                task_id,
                "已保存的图片缺少尺寸。",
                item=raw_item,
            )
        item = {
            "revised_prompt": str(raw_item.get("revised_prompt") or request_payload.get("prompt") or ""),
            "width": width,
            "height": height,
        }
        url = str(raw_item.get("url") or "").strip()
        relative_path = str(raw_item.get("relative_path") or "").strip()
        if legacy_import and response_format != "b64_json" and url:
            item["url"] = url
            image_urls.append(url)
            data.append(item)
            continue
        if legacy_import and response_format == "b64_json" and raw_item.get("b64_json"):
            item["b64_json"] = str(raw_item.get("b64_json") or "")
            if url:
                image_urls.append(url)
            data.append(item)
            continue
        if response_format != "b64_json" and is_url_only_result(raw_item):
            if not url:
                _invalid_image_result_error(
                    identity,
                    task_id,
                    "已保存的图片 URL 不可用。",
                    item=raw_item,
                )
            item["url"] = url
            image_urls.append(url)
            data.append(item)
            continue
        try:
            payload_bytes = image_task_service.read_result_artifact(identity, task_id, relative_path)
            route_verifier = getattr(
                image_task_service,
                "verify_public_image_route",
                None,
            )
            if response_format != "b64_json" and callable(route_verifier):
                route_verifier(relative_path)
        except Exception as exc:
            recover = getattr(image_task_service, "get_task", None)
            if callable(recover):
                try:
                    recover(identity, task_id)
                except Exception:
                    pass
            original = str(exc or "").strip() or IMAGE_RESULT_UNAVAILABLE_PUBLIC_MESSAGE
            raise _unavailable_image_result_error(original, task_id=task_id) from exc
        if response_format == "b64_json":
            item["b64_json"] = base64.b64encode(payload_bytes).decode("ascii")
        else:
            if not url:
                _invalid_image_result_error(
                    identity,
                    task_id,
                    "已保存的图片 URL 不可用。",
                    item=raw_item,
                )
            expected_path = f"/images/{relative_path.lstrip('/')}".rstrip("/")
            parsed_url = urlsplit(url)
            if parsed_url.path.rstrip("/") != expected_path:
                _invalid_image_result_error(
                    identity,
                    task_id,
                    "已保存的图片 URL 与产物路径不一致。",
                    item=raw_item,
                    extra=f"expected_path={expected_path}",
                )
            item["url"] = url
        if url:
            image_urls.append(url)
        data.append(item)
    expected_results = succeeded if is_partial else required
    if len(data) != expected_results:
        _raise_after_response_attempt(
            identity,
            task_id,
            ImageGenerationError(
                "图片任务返回的结果不完整。",
                failure=image_failure("image_job_failed"),
                task_id=task_id,
            ),
        )
    image_task_service.mark_response_attempted(identity, task_id)
    return {
        "created": int(time.time()),
        "data": data,
        "_image_urls": image_urls,
        "task_id": task_id,
        "_call_status": "partial_success" if is_partial else "success",
    }


async def prepare(
    body: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    prepared = body.get(_PREPARED_RESULT_KEY)
    if isinstance(prepared, dict):
        return prepared
    submission = await prepare_submission(
        body,
        payload,
        mode=mode,
        response_format=response_format,
    )
    identity = submission["identity"]
    request_payload = submission["request_payload"]
    task_id = str(submission["task_id"])
    response_format = _resolve_response_format(submission, request_payload, response_format)
    try:
        terminal = await image_task_service.wait_for_terminal_async(
            identity,
            task_id,
            timeout=_protocol_wait_timeout(),
        )
        prepared = await asyncio.to_thread(
            _result,
            identity,
            task_id,
            terminal,
            request_payload,
            response_format,
        )
    except Exception as exc:
        raise _task_error(exc, task_id) from exc
    body[_PREPARED_RESULT_KEY] = prepared
    return prepared


def execute(
    body: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    prepared = body.get(_PREPARED_RESULT_KEY)
    if isinstance(prepared, dict):
        return dict(prepared)
    submission = _submission(body, payload, mode, response_format)
    identity = submission["identity"]
    request_payload = submission["request_payload"]
    task_id = str(submission["task_id"])
    response_format = _resolve_response_format(submission, request_payload, response_format)
    try:
        terminal = _wait_for_terminal_from_worker_thread(
            identity,
            task_id,
            _protocol_wait_timeout(),
        )
        return _result(identity, task_id, terminal, request_payload, response_format)
    except Exception as exc:
        raise _task_error(exc, task_id) from exc


def submission_task_id(body: Mapping[str, Any]) -> str:
    submission = body.get(_SUBMISSION_KEY)
    return str(submission.get("task_id") or "") if isinstance(submission, dict) else ""


def attached_submission(body: Mapping[str, Any]) -> dict[str, Any] | None:
    submission = body.get(_SUBMISSION_KEY)
    if not isinstance(submission, dict):
        return None
    task_id = str(submission.get("task_id") or "").strip()
    identity = submission.get("identity")
    request_payload = submission.get("request_payload")
    if not task_id or not isinstance(identity, Mapping) or not isinstance(request_payload, Mapping):
        return None
    return submission


def attach_submission(body: dict[str, Any], submission: Mapping[str, Any]) -> None:
    task_id = str(submission.get("task_id") or "").strip()
    identity = submission.get("identity")
    request_payload = submission.get("request_payload")
    if not task_id or not isinstance(identity, Mapping) or not isinstance(request_payload, Mapping):
        return
    attached = {
        "identity": dict(identity),
        "request_payload": dict(request_payload),
        "task_id": task_id,
    }
    task_type = str(submission.get("task_type") or "").strip()
    if task_type:
        attached["task_type"] = task_type
    response_format = str(submission.get("response_format") or request_payload.get("response_format") or "").strip()
    if response_format:
        attached["response_format"] = response_format
    body[_SUBMISSION_KEY] = attached


def ensure_submission(
    body: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
) -> dict[str, Any]:
    return _submission(body, payload, mode, response_format)


_PROGRESS_SNAPSHOT_FIELDS = (
    "status",
    "stage",
    "progress",
    "queue_position",
    "estimated_wait_seconds",
    "succeeded_jobs",
    "failed_jobs",
    "required_jobs",
)


def _progress_fields(identity: Mapping[str, object], task_id: str) -> dict[str, Any]:
    """把任务快照里的排队/阶段字段带进心跳事件。

    心跳本身只是保活，附带这些字段后客户端才能显示排队位置和当前阶段。
    读取失败时静默降级为空字典，心跳不应该因为这个附加信息而中断。
    """
    reader = getattr(image_task_service, "progress_snapshot", None)
    if not callable(reader):
        return {}
    try:
        snapshot = reader(identity, task_id)
    except Exception:
        return {}
    if not isinstance(snapshot, Mapping):
        return {}
    fields: dict[str, Any] = {}
    for key in _PROGRESS_SNAPSHOT_FIELDS:
        value = snapshot.get(key)
        if value in (None, ""):
            continue
        if key == "status":
            fields["task_status"] = value
            continue
        fields[key] = value
    return fields


def _wait_for_terminal_from_worker_thread(
    identity: Mapping[str, object] | str,
    task_id: object,
    timeout: float | None,
) -> dict[str, Any]:
    wait_async = getattr(image_task_service, "wait_for_terminal_async", None)
    if callable(wait_async):
        try:
            import anyio

            return anyio.from_thread.run(wait_async, identity, task_id, timeout)
        except RuntimeError as exc:
            raise RuntimeError(
                "image task wait must run from a worker thread, not the event loop"
            ) from exc
    return image_task_service.wait_for_terminal(identity, task_id, timeout=timeout)


def stream_outputs(
    body: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    mode: str,
    response_format: str,
    model: str,
) -> Iterator[ImageOutput]:
    submission = _submission(body, payload, mode, response_format)
    identity = submission["identity"]
    request_payload = submission["request_payload"]
    task_id = str(submission["task_id"])
    response_format = _resolve_response_format(submission, request_payload, response_format)
    total = max(1, int(request_payload.get("n") or 1))
    yield ImageOutput(
        kind="progress",
        model=model,
        index=0,
        total=total,
        upstream_event_type="queued",
        task_id=task_id,
        progress_fields=_progress_fields(identity, task_id),
    )

    prepared = body.get(_PREPARED_RESULT_KEY)
    if isinstance(prepared, dict):
        yield as_output(prepared, model)
        return

    timeout = _protocol_wait_timeout()
    deadline = time.monotonic() + timeout
    heartbeat_seconds = min(15.0, max(1.0, timeout))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _task_error(TimeoutError(IMAGE_TASK_PENDING_PUBLIC_MESSAGE), task_id)
        try:
            terminal = _wait_for_terminal_from_worker_thread(
                identity,
                task_id,
                min(heartbeat_seconds, remaining),
            )
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise _task_error(TimeoutError(IMAGE_TASK_PENDING_PUBLIC_MESSAGE), task_id)
            yield ImageOutput(
                kind="progress",
                model=model,
                index=0,
                total=total,
                upstream_event_type="in_progress",
                task_id=task_id,
                progress_fields=_progress_fields(identity, task_id),
            )
            continue
        try:
            result = _result(
                identity,
                task_id,
                terminal,
                request_payload,
                response_format,
            )
        except Exception as exc:
            raise _task_error(exc, task_id) from exc
        body[_PREPARED_RESULT_KEY] = result
        yield as_output(result, model)
        return


def as_output(result: Mapping[str, Any], model: str) -> ImageOutput:
    return ImageOutput(
        kind="result",
        model=model,
        index=1,
        total=max(1, len(result.get("data") or [])),
        created=int(result.get("created") or time.time()),
        data=[dict(item) for item in result.get("data") or [] if isinstance(item, dict)],
        image_urls=[str(item) for item in result.get("_image_urls") or [] if str(item)],
        task_id=str(result.get("task_id") or ""),
        call_status=str(result.get("_call_status") or ""),
    )
