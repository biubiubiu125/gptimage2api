from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
from pathlib import Path
from threading import Lock
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from services.config import config
from services.image_delivery import (
    URL_ONLY_DELIVERY_MODE,
    is_url_only_delivery_mode,
    is_url_only_result,
    url_only_result_matches_base_url,
)
from services.image_failure import image_failure
from services.image_task_view import canonical_image_task_status
from services.image_url import build_public_image_url
from services.proxy_service import proxy_settings
from services.returned_url_verifier import verify_returned_image_url
from services.image_queue.artifact_service import ArtifactService, InvalidImageArtifact
from services.image_queue.database import ImageQueueDatabase, ImageQueueUnavailableError
from services.image_queue.idempotency import (
    build_effective_prompt,
    canonical_request_hash,
    require_public_image_model,
    select_idempotency_key,
)
from services.image_queue.repository import IdempotencyConflict, ImageQueueRepository
from services.image_queue.resource_controller import ImageQueueResourcePressureError, ImageQueueStorageFullError
from services.image_queue.sanitization import (
    safe_queue_error_message,
    sanitize_delivery_url,
    sanitize_trace_headers,
)
from services.image_queue.settings import ImageQueueConfigurationError, ImageQueueSettings
from services.image_queue.types import (
    ArtifactDescriptor,
    ArtifactStatus,
    ClaimedJob,
    DeliveryStatus,
    EnqueueRequest,
    JobCheckpoint,
    JobSnapshot,
    JobStage,
    JobStatus,
    LocalArtifactRecoveryUnavailable,
    TaskSnapshot,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
)
from utils.image_tokens import verify_image_bytes
from utils.log import logger


MAX_CLIENT_TASK_ID_LENGTH = 200


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _clean_client_task_id(value: object) -> str:
    client_task_id = _clean(value)
    if len(client_task_id) > MAX_CLIENT_TASK_ID_LENGTH:
        raise ValueError(f"client_task_id must be at most {MAX_CLIENT_TASK_ID_LENGTH} characters")
    return client_task_id


def _image_count(value: object) -> int:
    try:
        count = int(value or 1)
    except (TypeError, ValueError):
        count = 1
    return min(4, max(1, count))


def _owner_key(identity: Mapping[str, object] | str) -> str:
    if isinstance(identity, str):
        return identity.strip() or "anonymous"
    return _clean(identity.get("id") or identity.get("key") or identity.get("name"), "anonymous")


def _public_result_item(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    item = dict(value)
    for key in (
        "original_url",
        "originalUrl",
        "source_url",
        "sourceUrl",
        "_account_email",
    ):
        item.pop(key, None)
    return item


def _safe_task_error_message(error_code: object, error_message: object) -> str:
    code = _clean(error_code)
    raw_message = _clean(error_message)
    if not code and not raw_message:
        return ""
    failure = image_failure(code, raw_detail=raw_message)
    return safe_queue_error_message(
        RuntimeError(raw_message or code),
        failure,
    )


def _recovery_poll_timeout(payload: Mapping[str, Any] | None) -> float:
    request_payload = payload if isinstance(payload, Mapping) else {}
    try:
        requested = float(request_payload.get("resume_poll_timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        requested = 0.0
    if requested > 0:
        return requested
    try:
        configured = float(config.image_poll_timeout_secs)
    except (TypeError, ValueError):
        configured = 30.0
    return max(5.0, configured)


class ImageTaskService:
    def __init__(
        self,
        *,
        settings: ImageQueueSettings | None = None,
        database: ImageQueueDatabase | None = None,
        repository: ImageQueueRepository | None = None,
        artifact_service: ArtifactService | None = None,
        worker: Any | None = None,
        recovery: Any | None = None,
        job_generator: Any | None = None,
        conversation_cleanup: Any | None = None,
        upscaler: Any | None = None,
        backend_factory: Any | None = None,
        returned_url_verifier: Callable[..., None] | None = None,
    ) -> None:
        # Do not resolve the queue database during module import.  The module
        # exposes a process-wide service singleton, while API/test modules may
        # be imported before the PostgreSQL runtime is configured.  Startup
        # still resolves the settings through this property and fails there
        # with the normal queue configuration error.
        self._settings = settings
        self.database = database
        self.repository = repository
        self.artifact_service = artifact_service
        self.worker = worker
        self.recovery = recovery
        if job_generator is None or conversation_cleanup is None:
            from services.protocol.conversation import (
                cleanup_managed_image_conversation,
                generate_single_image_for_job,
            )

            job_generator = job_generator or generate_single_image_for_job
            conversation_cleanup = conversation_cleanup or cleanup_managed_image_conversation
        if upscaler is None:
            from services.image_upscale_service import upscale_image_with_status

            upscaler = upscale_image_with_status
        self.job_generator = job_generator
        self.conversation_cleanup = conversation_cleanup
        self.upscaler = upscaler
        self.returned_url_verifier = returned_url_verifier
        if backend_factory is None:
            from services.openai_backend_api import OpenAIBackendAPI

            backend_factory = OpenAIBackendAPI
        self.backend_factory = backend_factory
        self._owns_database = database is None
        self._started = False
        self._startup_error: Exception | None = None
        self._waiters_lock = Lock()
        self._terminal_waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    @property
    def settings(self) -> ImageQueueSettings:
        settings = self._settings
        if settings is None:
            settings = ImageQueueSettings.from_env()
            self._settings = settings
        return settings

    @settings.setter
    def settings(self, value: ImageQueueSettings) -> None:
        self._settings = value

    def _build_backend(
        self,
        access_token: str,
        deadline_monotonic: float = 0.0,
    ) -> Any:
        factory = self.backend_factory
        deadline = float(deadline_monotonic or 0.0)
        if deadline <= 0:
            return factory(access_token)
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return factory(access_token)
        accepts_deadline = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "deadline_monotonic"
            for parameter in parameters
        )
        if accepts_deadline:
            return factory(access_token, deadline_monotonic=deadline)
        return factory(access_token)

    def _notify_task_waiters(self, task_id: object) -> None:
        key = str(task_id or "")
        with self._waiters_lock:
            waiters = list(self._terminal_waiters.get(key, ()))
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                continue

    def _finalize_call_record_for_task(
        self,
        task: TaskSnapshot | None,
        *,
        call_id_override: object = "",
    ) -> None:
        if task is None or task.status not in TERMINAL_TASK_STATUSES:
            return
        request_payload = dict(task.request_payload or {})
        trace_headers = request_payload.get("trace_headers")
        call_id = _clean(
            call_id_override
            or request_payload.get("call_id")
            or (trace_headers.get("call_id") if isinstance(trace_headers, Mapping) else "")
        )
        if not call_id:
            return
        public_status = canonical_image_task_status(
            task.status.value,
            result_count=len(task.data),
            requested_count=task.required_jobs,
        )
        if public_status == "succeeded":
            call_status = "success"
        elif public_status == "partial_success":
            call_status = "partial_success"
        else:
            call_status = "failed"
        ended_at = task.completed_at or task.updated_at
        started_at = task.created_at
        try:
            request_started_timestamp = float(
                request_payload.get("request_started_at") or 0.0
            )
        except (TypeError, ValueError):
            request_started_timestamp = 0.0
        if request_started_timestamp > 0:
            started_at = datetime.fromtimestamp(
                request_started_timestamp,
                timezone.utc,
            )
        duration_ms = 0
        if started_at is not None and ended_at is not None:
            duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        image_urls = []
        for item in task.data:
            if not isinstance(item, Mapping):
                continue
            for key in ("url", "returned_url", "upscaled_url"):
                value = _clean(item.get(key))
                if value and value not in image_urls:
                    image_urls.append(value)
        error_code = _clean(task.error_code)
        if public_status == "cancelled" and not error_code:
            error_code = "image_task_cancelled"
        account_email = ""
        for item in task.data:
            if isinstance(item, Mapping):
                account_email = _clean(item.get("_account_email"))
                if account_email:
                    break
        detail: dict[str, Any] = {
            "call_id": call_id,
            "endpoint": _clean(request_payload.get("source_endpoint")) or (
                "/api/image-tasks/edits"
                if task.task_type == "edit"
                else "/api/image-tasks/generations"
            ),
            "model": task.public_model,
            "image_request": True,
            "status": call_status,
            "task_id": str(task.id),
            "task_status": public_status,
            "started_at": self._iso(started_at) or "",
            "ended_at": self._iso(ended_at) or "",
            "duration_ms": duration_ms,
            "image_requested_count": max(0, int(task.required_jobs)),
            "image_succeeded_count": len(task.data),
            "image_failed_count": max(0, int(task.required_jobs) - len(task.data)),
            "result_data_count": len(task.data),
            "result_url_count": len(image_urls),
            "image_result_status": (
                "success"
                if call_status == "success"
                else "partial_success"
                if call_status == "partial_success"
                else "failed"
            ),
            "image_urls": image_urls,
            "urls": image_urls,
        }
        if account_email:
            detail["account_email"] = account_email
        if task.conversation_id:
            detail["conversation_id"] = task.conversation_id
        if error_code:
            detail["error_code"] = error_code
            detail["error"] = _safe_task_error_message(error_code, task.error_message)
        try:
            from services.realtime_monitor_service import realtime_monitor_service

            realtime_monitor_service.finish(detail)
        except Exception as exc:
            logger.warning({
                "event": "image_task_realtime_monitor_finalize_failed",
                "task_id": str(task.id),
                "call_id": call_id,
                "error": str(exc),
            })
        try:
            from services.log_service import log_service

            summary = "调用完成" if call_status != "failed" else "调用失败"
            finalized = 0
            finalize_task = getattr(log_service, "finalize_task", None)
            if callable(finalize_task):
                finalized = int(finalize_task(str(task.id), summary=summary, detail=detail) or 0)
            if not finalized:
                log_service.finalize_call(
                    call_id,
                    summary=summary,
                    detail=detail,
                )
        except Exception as exc:
            logger.warning({
                "event": "image_task_call_record_finalize_failed",
                "task_id": str(task.id),
                "call_id": call_id,
                "error": str(exc),
            })

    def _finalize_call_record_for_task_id(
        self,
        task_id: object,
        *,
        call_id_override: object = "",
    ) -> None:
        repository = self.repository
        if repository is None:
            return
        try:
            task = repository.get_task_by_id(task_id)
        except Exception as exc:
            logger.warning({
                "event": "image_task_call_record_task_lookup_failed",
                "task_id": str(task_id or ""),
                "error": str(exc),
            })
            return
        self._finalize_call_record_for_task(task, call_id_override=call_id_override)

    def finalize_call_record(self, task_id: object, *, call_id: object = "") -> None:
        """Reconcile a task that may have reached terminal state before enqueue logging."""
        self._finalize_call_record_for_task_id(task_id, call_id_override=call_id)

    def _handle_task_state_change(self, task_id: object) -> None:
        self._notify_task_waiters(task_id)
        self._finalize_call_record_for_task_id(task_id)

    def _handle_task_state_change_safely(self, task_id: object) -> None:
        try:
            self._handle_task_state_change(task_id)
        except Exception as exc:
            logger.warning({
                "event": "image_task_state_change_reconciliation_failed",
                "task_id": str(task_id or ""),
                "error": str(exc),
            })

    def _worker_image_base_url(self) -> str:
        return _clean(config.base_url)

    def _delivery_base_url(self, payload: Mapping[str, Any]) -> str:
        return self._worker_image_base_url() or _clean(payload.get("base_url"))

    @staticmethod
    def _artifact_storage_service():
        from services.image_storage_service import image_storage_service

        return image_storage_service

    def _ensure_artifact_root_alignment(self) -> None:
        queue_root = self.settings.artifact_root.expanduser().resolve()
        public_root = config.images_dir.expanduser().resolve()
        if queue_root != public_root:
            raise ImageQueueConfigurationError(
                "image queue artifact root must match the public image storage root"
            )

    def _reconcile_user_quota_reservations(self) -> int:
        from services.quota_service import quota_service

        return quota_service.reconcile_task_reservations(
            self._require_repository().list_quota_reconciliation_tasks()
        )

    def _delivery_result_payload(
        self,
        artifact: ArtifactDescriptor,
        context: Mapping[str, Any],
        *,
        delivery_base_url: str,
        original_url: str = "",
        url_only_delivery: bool = False,
        account_email: str = "",
    ) -> dict[str, Any]:
        public_url = ""
        if url_only_delivery and delivery_base_url:
            public_url = build_public_image_url(delivery_base_url, artifact.relative_path)
        else:
            public_url = _clean(artifact.public_url)
            if not public_url and delivery_base_url:
                public_url = build_public_image_url(delivery_base_url, artifact.relative_path)
        result: dict[str, Any] = {
            "url": public_url,
            "width": artifact.width,
            "height": artifact.height,
            "revised_prompt": _clean(context.get("effective_prompt")),
            "relative_path": artifact.relative_path,
            "sha256": artifact.sha256,
            "storage_backend": artifact.storage_backend,
        }
        worker_id = _clean(getattr(self.worker, "worker_id", ""))
        if worker_id:
            result["worker_id"] = worker_id
        if delivery_base_url:
            result["image_base_url"] = delivery_base_url
        if public_url:
            result["returned_url"] = public_url
            result["upscaled_url"] = public_url
        if url_only_delivery:
            result["delivery_mode"] = URL_ONLY_DELIVERY_MODE
        if _clean(account_email):
            result["_account_email"] = _clean(account_email)
        return result

    def _require_repository(self) -> ImageQueueRepository:
        if self._startup_error is not None:
            raise ImageQueueUnavailableError("image queue did not start successfully") from self._startup_error
        if self.repository is None:
            raise ImageQueueUnavailableError("image queue PostgreSQL is not started")
        return self.repository

    def start(self) -> None:
        if self._started:
            return
        self._startup_error = None
        try:
            if self.database is None:
                self.database = ImageQueueDatabase(self.settings)
            self.database.start()
            self._ensure_artifact_root_alignment()
            if self.repository is None:
                self.repository = ImageQueueRepository(self.database)
            reconciled_reservations = self._reconcile_user_quota_reservations()
            if reconciled_reservations:
                logger.info({
                    "event": "image_queue_user_quota_reservations_reconciled",
                    "count": reconciled_reservations,
                })
            if self.artifact_service is None:
                # The in-process Image Queue owns public image files.  Do not
                # inherit a WebDAV/shared-image backend from a copied config file;
                # the result URL must remain bound to this app instance.
                storage_service = self._artifact_storage_service()
                self.artifact_service = ArtifactService(self.settings.artifact_root, storage_service)
            storage_cleanup = getattr(
                getattr(self.artifact_service, "storage_service", None),
                "cleanup_private_public_copies",
                None,
            )
            if callable(storage_cleanup):
                cleanup_result = storage_cleanup()
                if int(cleanup_result.get("failed") or 0) > 0:
                    logger.warning({
                        "event": "private_image_public_copy_cleanup_incomplete",
                        **cleanup_result,
                    })
            if self.recovery is None:
                from services.image_queue.recovery import ImageRecovery

                self.recovery = ImageRecovery(self.repository)
            import_legacy_tasks = getattr(self.recovery, "import_legacy_tasks", None)
            if callable(import_legacy_tasks):
                import_legacy_tasks(self.settings.legacy_task_path)
            self.recovery.recover()
            self._run_worker_maintenance()
            pending_ttl_seconds = int(self.settings.pending_ttl_seconds)
            if pending_ttl_seconds > 0:
                for task_id in self.repository.expire_pending_tasks(pending_ttl_seconds=pending_ttl_seconds):
                    self._handle_task_state_change(task_id)
            if self.worker is None:
                from services.account_service import account_service
                from services.image_queue.worker import ImageWorkerManager

                self.worker = ImageWorkerManager(
                    self.repository,
                    account_service,
                    self.execute_claim,
                    self.settings,
                    state_change_callback=self._handle_task_state_change,
                    local_recovery_callback=self._run_worker_maintenance,
                    local_recovery_available_callback=self.local_recovery_artifacts_available,
                    terminal_cleanup_callback=self.artifact_service.discard,
                )
            if self.worker is not None:
                self.worker.start()
            self._started = True
            logger.info({
                "event": "image_queue_started",
                "database_configured": bool(self.settings.database_url),
                "artifact_root": str(self.settings.artifact_root),
                "instance_id": str(getattr(self.settings, "instance_id", "") or ""),
                "lease_seconds": int(self.settings.lease_seconds),
                "generation_attempts": int(self.settings.generation_attempts),
                "download_attempts": int(self.settings.download_attempts),
                "save_attempts": int(self.settings.save_attempts),
            })
        except Exception as exc:
            self._startup_error = exc
            raise

    def stop(self, timeout: float | None = None) -> None:
        drained = True
        if self.worker is not None:
            drained = self.worker.stop(timeout) is not False
        self._started = False
        if not drained:
            # A timed-out worker owns executor threads that cannot be reused
            # safely for a later lifecycle.  Detach it so the next start builds
            # a fresh manager; the old claims are fenced by their lease token.
            logger.warning({
                "event": "image_queue_worker_detached_after_shutdown_timeout",
            })
            self.worker = None
        if self._owns_database and self.database is not None and drained:
            self.database.dispose()
        elif self._owns_database and self.database is not None:
            logger.warning({
                "event": "image_queue_shutdown_database_kept_alive",
                "reason": "active image jobs did not reach a safe checkpoint before timeout",
            })

    def health_summary(self) -> dict[str, object]:
        if self._startup_error is not None:
            return {
                "status": "unavailable",
                "healthy": False,
                "error": str(self._startup_error),
            }
        worker_fatal_error = str(getattr(self.worker, "fatal_error", "") or "").strip()
        if worker_fatal_error:
            return {
                "status": "unavailable",
                "healthy": False,
                "error": worker_fatal_error,
            }
        if not self._started:
            return {
                "status": "starting",
                "healthy": False,
                "error": "image queue is not started",
            }
        if self._started and self.repository is None:
            return {
                "status": "unavailable",
                "healthy": False,
                "error": "image queue repository is not started",
            }
        if self._started:
            if self.database is None:
                return {
                    "status": "unavailable",
                    "healthy": False,
                    "error": "image queue database is not started",
                }
            try:
                self.database.ping()
            except Exception as exc:
                return {
                    "status": "unavailable",
                    "healthy": False,
                    "error": str(exc),
                }
        return {"status": "ok", "healthy": True}

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _public_snapshot(
        self,
        snapshot: TaskSnapshot,
        position: int | None = None,
        worker_pause_reason: str | None = None,
    ) -> dict[str, Any]:
        repository = self._require_repository()
        request_payload = dict(snapshot.request_payload or {})
        public_status = canonical_image_task_status(
            snapshot.status.value,
            result_count=len(snapshot.data),
            requested_count=snapshot.required_jobs,
        )
        if position is None:
            position = (
                0
                if snapshot.status in TERMINAL_TASK_STATUSES
                else repository.queue_position(snapshot.id)
            )
        queued_jobs_ahead = max(0, position - 1)
        estimate_seconds = queued_jobs_ahead * 30
        now = datetime.now(timezone.utc)
        wait_reason = snapshot.wait_reason or ("queued" if position else "")
        if snapshot.status == TaskStatus.QUEUED and wait_reason in {"", "queued"}:
            if worker_pause_reason is None:
                worker_pause_reason = repository.current_worker_pause_reason()
            wait_reason = worker_pause_reason or wait_reason
        public_error = _safe_task_error_message(
            snapshot.error_code,
            snapshot.error_message,
        )
        return {
            "id": str(snapshot.id),
            "task_id": str(snapshot.id),
            "client_task_id": snapshot.client_task_id,
            "status": public_status,
            "mode": "edit" if snapshot.task_type == "edit" else "generate",
            "model": snapshot.public_model,
            "n": snapshot.required_jobs,
            "size": request_payload.get("size"),
            "quality": _clean(request_payload.get("quality"), "auto"),
            "required_jobs": snapshot.required_jobs,
            "succeeded_jobs": snapshot.succeeded_jobs,
            "failed_jobs": snapshot.failed_jobs,
            "queue_position": position,
            "estimated_wait_seconds": estimate_seconds,
            "estimated_start_at": (now + timedelta(seconds=estimate_seconds)).isoformat() if position else None,
            "wait_reason": wait_reason,
            "delivery_status": snapshot.delivery_status.value,
            "stage": snapshot.stage,
            "progress": snapshot.progress or snapshot.stage or public_status,
            "conversation_id": snapshot.conversation_id,
            "can_resume_poll": bool(snapshot.can_resume_poll),
            "cancel_requested": bool(snapshot.cancel_requested),
            "data": [_public_result_item(item) for item in snapshot.data],
            "error_code": snapshot.error_code,
            "error": public_error,
            "created_at": self._iso(snapshot.created_at),
            "updated_at": self._iso(snapshot.updated_at),
        }

    def _ensure_deliverable_task(
        self,
        owner: str,
        snapshot: TaskSnapshot,
        *,
        force_url_verification: bool = False,
    ) -> TaskSnapshot:
        terminal_with_results = (
            snapshot.status in {TaskStatus.FAILED, TaskStatus.CANCELED}
            and snapshot.succeeded_jobs > 0
        )
        if (
            (snapshot.status != TaskStatus.SUCCESS and not terminal_with_results)
            or not snapshot.data
        ):
            return snapshot
        if bool(snapshot.request_payload.get("legacy_import")):
            return snapshot
        if self.artifact_service is None:
            raise ImageQueueUnavailableError("image artifact service is not started")
        repository = self._require_repository()
        artifact_history = repository.list_artifacts(snapshot.id)
        recovery_not_after = snapshot.completed_at or snapshot.updated_at
        verified_delivery_job_ids: set[UUID] = set()
        failed_delivery_job_ids: set[UUID] = set()
        failed_delivery_worker_ids: set[str] = set()
        delivery_failure_snapshot: TaskSnapshot | None = None
        delivery_failure_error: Exception | None = None
        for item in snapshot.data:
            explicit_url_only = is_url_only_delivery_mode(item.get("delivery_mode"))
            url_only_result = is_url_only_result(item)
            if explicit_url_only and not url_only_result:
                raise InvalidImageArtifact("invalid image result")
            if url_only_result:
                artifact = self._delivery_artifact(item, artifact_history)
                if artifact is None or artifact.job_id is None or artifact.status != ArtifactStatus.READY:
                    missing_artifact_error = InvalidImageArtifact("image result artifact not found")
                    failed = self._fail_missing_artifact_result(
                        owner,
                        snapshot,
                        _clean(item.get("relative_path")),
                        missing_artifact_error,
                        artifact=artifact,
                    )
                    if failed is None:
                        raise missing_artifact_error
                    self._handle_task_state_change_safely(failed.id)
                    delivery_failure_snapshot = failed
                    delivery_failure_error = delivery_failure_error or missing_artifact_error
                    resolved_job_id = self._result_job_id_for_path(
                        owner,
                        snapshot,
                        _clean(item.get("relative_path")),
                        artifact=artifact,
                    )
                    if resolved_job_id is not None:
                        failed_delivery_job_ids.add(resolved_job_id)
                    continue
                if not self._delivery_worker_id(item, artifact_history):
                    raise InvalidImageArtifact("invalid image result")
                try:
                    allowed_base_url = self._returned_url_allowed_base(item, artifact_history)
                except Exception as exc:
                    raise InvalidImageArtifact("invalid image result") from exc
                if not allowed_base_url:
                    raise InvalidImageArtifact("invalid image result")
                if not url_only_result_matches_base_url(item, allowed_base_url):
                    raise InvalidImageArtifact("invalid image result")
                if (
                    self.settings.verify_returned_url
                    and snapshot.delivery_status != DeliveryStatus.ACKNOWLEDGED
                    and (force_url_verification or snapshot.delivery_status == DeliveryStatus.PENDING)
                ):
                    returned_url = _clean(item.get("returned_url") or item.get("url"))
                    try:
                        self._verify_returned_url(returned_url, allowed_base_url=allowed_base_url)
                    except Exception as exc:
                        worker_id = self._delivery_worker_id(item, artifact_history)
                        if worker_id:
                            failed_delivery_worker_ids.add(worker_id)
                        self._record_worker_delivery_status(
                            item,
                            returned_url,
                            artifact_history,
                            healthy=False,
                            error=str(exc),
                        )
                        failed = self._fail_undeliverable_url_result(
                            owner,
                            snapshot,
                            item,
                            returned_url,
                            exc,
                            artifact_history,
                        )
                        if failed is None:
                            raise
                        self._handle_task_state_change_safely(failed.id)
                        if artifact is not None and artifact.job_id is not None:
                            failed_delivery_job_ids.add(artifact.job_id)
                        delivery_failure_snapshot = failed
                        delivery_failure_error = delivery_failure_error or exc
                        continue
                    worker_id = self._delivery_worker_id(item, artifact_history)
                    if worker_id not in failed_delivery_worker_ids:
                        self._record_worker_delivery_status(
                            item,
                            returned_url,
                            artifact_history,
                            healthy=True,
                        )
                if artifact is not None and artifact.job_id is not None:
                    verified_delivery_job_ids.add(artifact.job_id)
                continue
            relative_path = _clean(item.get("relative_path"))
            artifact = next(
                (
                    value
                    for value in artifact_history
                    if value.kind == "final" and value.relative_path == relative_path
                ),
                None,
            )
            if artifact is None:
                missing_artifact_error = InvalidImageArtifact("image result artifact row is missing")
                resolved_job_id = self._result_job_id_for_path(
                    owner,
                    snapshot,
                    relative_path,
                )
                failed = self._fail_missing_artifact_result(
                    owner,
                    snapshot,
                    relative_path,
                    missing_artifact_error,
                )
                if failed is None:
                    raise missing_artifact_error
                delivery_failure_snapshot = failed
                delivery_failure_error = delivery_failure_error or missing_artifact_error
                self._handle_task_state_change_safely(failed.id)
                if resolved_job_id is not None:
                    failed_delivery_job_ids.add(resolved_job_id)
                continue
            try:
                self.read_result_artifact(owner, snapshot.id, relative_path)
                public_url = _clean(
                    item.get("returned_url")
                    or item.get("url")
                    or getattr(artifact, "public_url", "")
                )
                if (
                    public_url
                    and _clean(getattr(artifact, "storage_backend", "")).lower()
                    not in {"webdav", "both"}
                    and self.settings.verify_returned_url
                    and snapshot.delivery_status != DeliveryStatus.ACKNOWLEDGED
                    and (force_url_verification or snapshot.delivery_status == DeliveryStatus.PENDING)
                ):
                    allowed_base_url = self._returned_url_allowed_base(
                        item,
                        artifact_history,
                    )
                    try:
                        self._verify_returned_url(
                            public_url,
                            allowed_base_url=allowed_base_url,
                        )
                    except Exception as exc:
                        failed = self._fail_undeliverable_url_result(
                            owner,
                            snapshot,
                            item,
                            public_url,
                            exc,
                            artifact_history,
                        )
                        if failed is None:
                            raise
                        self._handle_task_state_change_safely(failed.id)
                        if artifact is not None and artifact.job_id is not None:
                            failed_delivery_job_ids.add(artifact.job_id)
                        delivery_failure_snapshot = failed
                        delivery_failure_error = delivery_failure_error or exc
                        continue
                remote_public_url = self._remote_public_artifact_url(item, artifact)
                if (
                    remote_public_url
                    and self.settings.verify_returned_url
                    and snapshot.delivery_status != DeliveryStatus.ACKNOWLEDGED
                    and (force_url_verification or snapshot.delivery_status == DeliveryStatus.PENDING)
                ):
                    try:
                        self._verify_returned_url(remote_public_url)
                    except Exception as exc:
                        failed = self._fail_undeliverable_url_result(
                            owner,
                            snapshot,
                            item,
                            remote_public_url,
                            exc,
                            artifact_history,
                        )
                        if failed is None:
                            raise
                        self._handle_task_state_change_safely(failed.id)
                        if artifact is not None and artifact.job_id is not None:
                            failed_delivery_job_ids.add(artifact.job_id)
                        delivery_failure_snapshot = failed
                        delivery_failure_error = delivery_failure_error or exc
                        continue
                if artifact is not None and artifact.job_id is not None:
                    verified_delivery_job_ids.add(artifact.job_id)
                continue
            except InvalidImageArtifact as exc:
                if artifact is None or artifact.job_id is None:
                    raise
                requeue_artifacts = tuple(
                    value
                    for value in artifact_history
                    if value.job_id == artifact.job_id
                    and value.kind in {"final", "upscaled", "downloaded"}
                )
                local_recovery_kind = ""
                for kind in ("upscaled", "downloaded"):
                    preferred_sha256 = next(
                        (
                            value.sha256
                            for value in reversed(artifact_history)
                            if value.job_id == artifact.job_id
                            and value.kind == kind
                            and value.status == ArtifactStatus.READY
                        ),
                        "",
                    )
                    recovered = self._run_in_worker_pool(
                        "io",
                        lambda kind=kind, preferred_sha256=preferred_sha256: self.artifact_service.recover_stage(
                            snapshot.id,
                            artifact.job_id,
                            kind,
                            preferred_sha256=preferred_sha256,
                            not_after=recovery_not_after,
                        ),
                    )
                    if recovered is not None:
                        local_recovery_kind = kind
                        break
                recovering = repository.requeue_undeliverable_result(
                    owner,
                    snapshot.id,
                    artifact.job_id,
                    local_recovery_kind=local_recovery_kind,
                    error_message=str(exc),
                )
                if recovering is None:
                    failed = repository.fail_undeliverable_result(
                        owner,
                        snapshot.id,
                        artifact.job_id,
                        error_code="invalid_image_result",
                        error_message=_safe_task_error_message(
                            "invalid_image_result",
                            str(exc),
                        ),
                    )
                    if failed is None:
                        raise
                    self._handle_task_state_change_safely(failed.id)
                    failed_delivery_job_ids.add(artifact.job_id)
                    delivery_failure_snapshot = failed
                    delivery_failure_error = delivery_failure_error or exc
                    continue
                self._discard_requeued_artifacts(snapshot.id, requeue_artifacts)
                if self.worker is not None:
                    self.worker.notify()
                self._handle_task_state_change_safely(recovering.id)
                return recovering
        if delivery_failure_snapshot is not None:
            if verified_delivery_job_ids:
                self._record_task_quota_accounting(
                    owner,
                    delivery_failure_snapshot,
                    verified_success=True,
                    job_ids=verified_delivery_job_ids,
                )
            if failed_delivery_job_ids:
                self._record_task_quota_accounting(
                    owner,
                    delivery_failure_snapshot,
                    verified_success=False,
                    error=delivery_failure_error,
                    job_ids=failed_delivery_job_ids,
                )
            return delivery_failure_snapshot
        self._record_task_quota_accounting(owner, snapshot, verified_success=True)
        return snapshot

    @staticmethod
    def _verifier_accepts_allowed_base_url(verifier: Callable[..., None]) -> bool:
        try:
            parameters = inspect.signature(verifier).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == "allowed_base_url"
            for parameter in parameters
        )

    def _verify_returned_url(self, url: str, *, allowed_base_url: str = "") -> None:
        verifier = self.returned_url_verifier
        if verifier is not None:
            if self._verifier_accepts_allowed_base_url(verifier):
                verifier(url, allowed_base_url=allowed_base_url)
            else:
                verifier(url)
            return
        profile = proxy_settings.get_profile(resource=True, upstream=True)
        if profile.clearance_enabled:
            proxy_settings.refresh_clearance(
                target_url=url,
                resource=True,
                upstream=True,
            )
        verify_returned_image_url(
            url,
            timeout_seconds=self.settings.returned_url_verify_timeout_seconds,
            attempts=self.settings.returned_url_verify_attempts,
            max_bytes=self.settings.returned_url_verify_max_bytes,
            allowed_base_url=allowed_base_url,
            proxy=profile.proxy_url,
            verify=not profile.skip_ssl_verify,
        )

    @staticmethod
    def _remote_public_artifact_url(
        item: Mapping[str, Any],
        artifact: ArtifactDescriptor | None,
    ) -> str:
        if artifact is None:
            return ""
        storage_backend = _clean(getattr(artifact, "storage_backend", "")).lower()
        if storage_backend not in {"webdav", "both"}:
            return ""
        url = _clean(item.get("returned_url") or item.get("url") or getattr(artifact, "public_url", ""))
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return url

    @staticmethod
    def _delivery_artifact(
        item: Mapping[str, Any],
        artifact_history: Sequence[ArtifactDescriptor],
    ) -> ArtifactDescriptor | None:
        relative_path = _clean(item.get("relative_path"))
        if not relative_path:
            return None
        return next(
            (
                artifact
                for artifact in artifact_history
                if artifact.kind == "final"
                and artifact.relative_path == relative_path
            ),
            None,
        )

    @classmethod
    def _delivery_worker_id(
        cls,
        item: Mapping[str, Any],
        artifact_history: Sequence[ArtifactDescriptor],
    ) -> str:
        artifact = cls._delivery_artifact(item, artifact_history)
        artifact_worker_id = _clean(getattr(artifact, "worker_id", ""))
        if artifact_worker_id:
            return artifact_worker_id
        return _clean(item.get("worker_id"))

    def _record_worker_delivery_status(
        self,
        item: Mapping[str, Any],
        returned_url: str,
        artifact_history: Sequence[ArtifactDescriptor],
        *,
        healthy: bool,
        error: str = "",
    ) -> None:
        worker_id = self._delivery_worker_id(item, artifact_history)
        if not worker_id:
            return
        safe_url = sanitize_delivery_url(returned_url)
        safe_error = (
            _safe_task_error_message("image_url_unreachable", error)
            if error
            else ""
        )
        try:
            self._require_repository().record_worker_delivery_status(
                worker_id,
                healthy=healthy,
                url=safe_url,
                error=safe_error,
            )
        except Exception as exc:
            logger.warning({
                "event": "worker_delivery_status_record_failed",
                "worker_id": worker_id,
                "url": safe_url,
                "healthy": healthy,
                "error": str(exc),
            })

    def _returned_url_allowed_base(
        self,
        item: Mapping[str, Any],
        artifact_history: Sequence[ArtifactDescriptor],
    ) -> str:
        worker_id = self._delivery_worker_id(item, artifact_history)
        if worker_id:
            try:
                lookup = getattr(self._require_repository(), "worker_image_base_url", None)
                if callable(lookup):
                    image_base_url = _clean(lookup(worker_id))
                    if image_base_url:
                        return image_base_url
                    persisted_base_url = _clean(item.get("image_base_url"))
                    if persisted_base_url:
                        return persisted_base_url
                    raise RuntimeError(f"worker image base URL is not registered for {worker_id}")
            except Exception as exc:
                logger.warning({
                    "event": "worker_image_base_url_lookup_failed",
                    "worker_id": worker_id,
                    "error": str(exc),
                })
                raise
        return _clean(item.get("image_base_url"))

    def _record_task_quota_accounting(
        self,
        owner: str,
        snapshot: TaskSnapshot,
        *,
        verified_success: bool,
        error: Exception | None = None,
        job_ids: set[UUID] | None = None,
    ) -> None:
        try:
            jobs = self._require_repository().list_unaccounted_quota_jobs_for_task(owner, snapshot.id)
        except Exception as exc:
            logger.error({
                "event": "image_task_quota_lookup_failed",
                "task_id": str(snapshot.id),
                "error": str(exc),
            })
            return
        if job_ids is not None:
            jobs = [job for job in jobs if job.id in job_ids]
        if not jobs:
            return
        repository = self._require_repository()

        def note_quota_accounting_failure(job: JobSnapshot, message: object) -> None:
            if job.id is None:
                return
            record_failure = getattr(repository, "record_quota_accounting_failure", None)
            if not callable(record_failure):
                return
            try:
                record_failure(job.id, str(message or "quota accounting failed"))
            except Exception as exc:
                logger.error({
                    "event": "image_task_quota_failure_state_persist_failed",
                    "task_id": str(snapshot.id),
                    "job_id": str(job.id),
                    "error": str(exc),
                })

        try:
            from services import account_service as account_service_module
        except Exception as exc:
            logger.error({
                "event": "image_task_quota_account_service_unavailable",
                "task_id": str(snapshot.id),
                "error": str(exc),
            })
            for job in jobs:
                note_quota_accounting_failure(job, str(exc))
            return
        account_service = getattr(account_service_module, "account_service", None)
        recorder = getattr(account_service, "record_managed_image_result", None)

        if not callable(recorder):
            logger.error({
                "event": "image_task_quota_account_recorder_unavailable",
                "task_id": str(snapshot.id),
            })
            for job in jobs:
                note_quota_accounting_failure(job, "quota account recorder unavailable")
            return
        for job in jobs:
            if job.account_id is None:
                note_quota_accounting_failure(job, "image job has no account id")
                continue
            success = bool(verified_success and job.status == JobStatus.SUCCESS)
            values: dict[str, Any] = {
                "success": success,
                "quota_consumed": True,
                "idempotency_key": f"image-job:{job.id}",
            }
            if not success and error is not None:
                values["error"] = error
            try:
                recorded = recorder(job.account_id, **values)
            except Exception as exc:
                logger.error({
                    "event": "image_task_quota_accounting_failed",
                    "task_id": str(snapshot.id),
                    "job_id": str(job.id),
                    "account_id": str(job.account_id),
                    "success": success,
                    "error": str(exc),
                })
                note_quota_accounting_failure(job, str(exc))
                continue
            if recorded:
                repository.mark_quota_accounted(job.id, job.account_id)
            else:
                note_quota_accounting_failure(job, "account result recorder returned no result")

    def _fail_undeliverable_url_result(
        self,
        owner: str,
        snapshot: TaskSnapshot,
        item: Mapping[str, Any],
        returned_url: str,
        exc: Exception,
        artifact_history: Sequence[ArtifactDescriptor],
    ) -> TaskSnapshot | None:
        relative_path = _clean(item.get("relative_path"))
        artifact = next(
            (
                value
                for value in artifact_history
                if value.kind == "final" and value.relative_path == relative_path
            ),
            None,
        )
        if artifact is None or artifact.job_id is None:
            return None
        message = (
            f"returned image URL is not reachable: {returned_url}; {exc}"
            if returned_url
            else f"returned image URL is empty or invalid; {exc}"
        )
        return self._require_repository().fail_undeliverable_result(
            owner,
            snapshot.id,
            artifact.job_id,
            error_code="image_url_unreachable",
            error_message=_safe_task_error_message("image_url_unreachable", message),
        )

    def _result_job_id_for_path(
        self,
        owner: str,
        snapshot: TaskSnapshot,
        relative_path: str,
        *,
        artifact: ArtifactDescriptor | None = None,
    ) -> UUID | None:
        job_id = artifact.job_id if artifact is not None else None
        if job_id is None:
            finder = getattr(self._require_repository(), "find_job_for_result_path", None)
            if callable(finder):
                try:
                    owner_job = finder(owner, snapshot.id, relative_path)
                except Exception:
                    owner_job = None
                job_id = getattr(owner_job, "id", None)
        return job_id

    def _fail_missing_artifact_result(
        self,
        owner: str,
        snapshot: TaskSnapshot,
        relative_path: str,
        error: Exception,
        *,
        artifact: ArtifactDescriptor | None = None,
    ) -> TaskSnapshot | None:
        job_id = self._result_job_id_for_path(
            owner,
            snapshot,
            relative_path,
            artifact=artifact,
        )
        if job_id is None:
            return None
        return self._require_repository().fail_undeliverable_result(
            owner,
            snapshot.id,
            job_id,
            error_code="invalid_image_result",
            error_message=_safe_task_error_message("invalid_image_result", str(error)),
        )

    @staticmethod
    def _has_deliverable_result(snapshot: TaskSnapshot) -> bool:
        if not snapshot.data:
            return False
        if snapshot.status == TaskStatus.SUCCESS:
            return True
        return (
            snapshot.status in {TaskStatus.FAILED, TaskStatus.CANCELED}
            and snapshot.succeeded_jobs > 0
        )

    def _mark_response_attempted_for_deliverable_result(
        self,
        owner: str,
        snapshot: TaskSnapshot,
    ) -> TaskSnapshot:
        if (
            not self._has_deliverable_result(snapshot)
            or snapshot.delivery_status != DeliveryStatus.PENDING
        ):
            return snapshot
        updated = self._require_repository().mark_response_attempted(owner, snapshot.id)
        return updated or snapshot

    def _persist_inputs(
        self,
        task_id: UUID,
        images: list[tuple[bytes, str, str]] | None,
        masks: list[tuple[bytes, str, str]] | None,
    ) -> tuple[list[str], list[str], tuple[Any, ...]]:
        if self.artifact_service is None:
            raise ImageQueueUnavailableError("image artifact service is not started")
        descriptors = []
        image_paths = []
        mask_paths = []
        try:
            for ordinal, (payload, filename, mime_type) in enumerate(images or [], start=1):
                artifact = self._run_in_worker_pool(
                    "io",
                    lambda payload=payload, filename=filename, mime_type=mime_type, ordinal=ordinal: self.artifact_service.persist_input(
                        task_id, payload, filename, mime_type, kind="input", ordinal=ordinal
                    ),
                )
                descriptors.append(artifact)
                image_paths.append(artifact.relative_path)
            for ordinal, (payload, filename, mime_type) in enumerate(masks or [], start=1):
                artifact = self._run_in_worker_pool(
                    "io",
                    lambda payload=payload, filename=filename, mime_type=mime_type, ordinal=ordinal: self.artifact_service.persist_input(
                        task_id, payload, filename, mime_type, kind="mask", ordinal=ordinal
                    ),
                )
                descriptors.append(artifact)
                mask_paths.append(artifact.relative_path)
        except Exception:
            try:
                self.artifact_service.discard(tuple(descriptors))
            except Exception as cleanup_exc:
                logger.warning({
                    "event": "image_input_artifact_cleanup_failed",
                    "task_id": str(task_id),
                    "error": str(cleanup_exc),
                })
            raise
        return image_paths, mask_paths, tuple(descriptors)

    def _discard_requeued_artifacts(
        self,
        task_id: UUID,
        candidates: Sequence[ArtifactDescriptor],
    ) -> None:
        if self.artifact_service is None or not candidates:
            return
        try:
            remaining_paths = {
                artifact.relative_path
                for artifact in self._require_repository().list_artifacts(task_id)
                if artifact.status != ArtifactStatus.INVALID
            }
        except Exception as exc:
            logger.warning({
                "event": "image_requeued_artifact_cleanup_skipped",
                "task_id": str(task_id),
                "error": str(exc),
            })
            return
        discarded = tuple(
            artifact
            for artifact in candidates
            if artifact.relative_path not in remaining_paths
        )
        if not discarded:
            return
        try:
            deleted = self.artifact_service.discard(discarded)
        except Exception as exc:
            logger.warning({
                "event": "image_requeued_artifact_cleanup_failed",
                "task_id": str(task_id),
                "artifact_count": len(discarded),
                "error": str(exc),
            })
            return
        if not deleted:
            logger.warning({
                "event": "image_requeued_artifact_cleanup_incomplete",
                "task_id": str(task_id),
                "artifact_count": len(discarded),
            })
            return
        try:
            self._require_repository().remove_invalid_artifacts(
                task_id,
                tuple(artifact.relative_path for artifact in discarded),
            )
        except Exception as exc:
            logger.warning({
                "event": "image_requeued_artifact_index_cleanup_failed",
                "task_id": str(task_id),
                "artifact_count": len(discarded),
                "error": str(exc),
            })

    def _run_in_worker_pool(self, pool: str, operation):
        submit = getattr(self.worker, f"submit_{pool}", None)
        if callable(submit):
            return submit(operation).result()
        return operation()

    def _ensure_submission_capacity(self) -> None:
        controller = getattr(self.worker, "resource_controller", None)
        if controller is None:
            return
        decision = controller.allow_new_submission(controller.sample())
        if not decision.allowed:
            if decision.reason == "resource_disk":
                raise ImageQueueStorageFullError()
            raise ImageQueueResourcePressureError(decision.reason or "resource_pressure")

    def _ensure_backlog_capacity(self, repository: ImageQueueRepository) -> None:
        max_backlog = max(1, int(getattr(self.settings, "max_backlog", 0) or 0))
        if repository.count_backlog_tasks() >= max_backlog:
            raise ImageQueueResourcePressureError("resource_backlog")

    def _enqueue_accepts_max_backlog(self, repository: ImageQueueRepository) -> bool:
        try:
            signature = inspect.signature(repository.enqueue_task)
        except (TypeError, ValueError):
            return True
        parameters = signature.parameters.values()
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == "max_backlog"
            for parameter in parameters
        )

    def _read_input_artifacts(self, task_id: UUID, kind: str) -> list[tuple[bytes, str, str]]:
        if self.artifact_service is None:
            raise ImageQueueUnavailableError("image artifact service is not started")
        resolved_root = self.artifact_service.root.resolve()
        storage_service = getattr(self.artifact_service, "storage_service", None)
        remote_reader = getattr(storage_service, "get_remote_bytes", None) if storage_service is not None else None
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_artifact_bytes", None)
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_published_bytes", None)
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_bytes", None)
        queue_payload_reader = getattr(self._require_repository(), "get_artifact_payload", None)
        items: list[tuple[bytes, str, str]] = []
        artifacts = sorted(
            (
                artifact
                for artifact in self._require_repository().list_artifacts(task_id)
                if artifact.kind == kind and artifact.status == ArtifactStatus.READY
            ),
            key=lambda artifact: (
                artifact.ordinal if artifact.ordinal is not None else 2**31,
                artifact.relative_path,
            ),
        )
        for artifact in artifacts:
            path = (resolved_root / artifact.relative_path).resolve()
            try:
                path.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError("input artifact path escapes storage root") from exc

            def verify_payload(payload: bytes) -> bytes:
                try:
                    verified = verify_image_bytes(payload)
                except ValueError as exc:
                    raise InvalidImageArtifact("input artifact is unreadable") from exc
                if not payload or len(payload) != artifact.byte_size:
                    raise InvalidImageArtifact("input artifact size mismatch")
                if sha256(payload).hexdigest() != artifact.sha256:
                    raise InvalidImageArtifact("input artifact checksum mismatch")
                if (verified.width, verified.height) != (artifact.width, artifact.height):
                    raise InvalidImageArtifact("input artifact dimensions mismatch")
                return payload

            def read_verified() -> bytes:
                try:
                    return verify_payload(self.artifact_service.read(artifact))
                except InvalidImageArtifact:
                    if not callable(remote_reader) and not callable(queue_payload_reader):
                        raise
                except OSError:
                    if not callable(remote_reader) and not callable(queue_payload_reader):
                        raise InvalidImageArtifact("input artifact is unreadable")
                if callable(remote_reader):
                    try:
                        return verify_payload(remote_reader(artifact.relative_path))
                    except InvalidImageArtifact:
                        if not callable(queue_payload_reader):
                            raise
                    except Exception:
                        pass
                if callable(queue_payload_reader):
                    try:
                        return verify_payload(queue_payload_reader(artifact.relative_path))
                    except InvalidImageArtifact:
                        raise
                    except Exception as exc:
                        raise InvalidImageArtifact("input artifact is unreadable") from exc
                raise InvalidImageArtifact("input artifact is unreadable")

            payload = self._run_in_worker_pool("io", read_verified)
            items.append((payload, path.name, artifact.mime_type))
        return items

    def adopt_local_recovery_artifacts(self) -> int:
        repository = self._require_repository()
        if self.artifact_service is None:
            return 0
        worker_id = _clean(getattr(self.worker, "worker_id", ""))
        adopted = 0
        for job in repository.list_local_recovery_candidates():
            for artifact in self.artifact_service.recover_local_artifacts(
                job.task_id,
                job.id,
                not_after=job.available_at,
            ):
                if repository.adopt_recovery_artifact(
                    job.id,
                    artifact,
                    worker_id=worker_id,
                ):
                    adopted += 1
        return adopted

    def reconcile_unaccounted_delivery_results(self, limit: int = 100) -> int:
        repository = self._require_repository()
        task_ids: list[UUID] = []
        seen_task_ids: set[UUID] = set()
        for job in repository.list_unaccounted_terminal_quota_jobs(limit=limit):
            if job.task_id in seen_task_ids:
                continue
            seen_task_ids.add(job.task_id)
            task_ids.append(job.task_id)
        accounted = 0
        for task_id in task_ids:
            snapshot = repository.get_task_by_id(task_id)
            if snapshot is None:
                continue
            before = {
                job.id
                for job in repository.list_unaccounted_quota_jobs_for_task(
                    snapshot.owner_key,
                    snapshot.id,
                )
            }
            if not before:
                continue
            try:
                self._ensure_deliverable_task(
                    snapshot.owner_key,
                    snapshot,
                    force_url_verification=True,
                )
            except Exception as exc:
                logger.warning({
                    "event": "image_task_delivery_reconciliation_failed",
                    "task_id": str(snapshot.id),
                    "error": str(exc),
                })
                continue
            after = {
                job.id
                for job in repository.list_unaccounted_quota_jobs_for_task(
                    snapshot.owner_key,
                    snapshot.id,
                )
            }
            accounted += len(before - after)
        return accounted

    def _run_worker_maintenance(self) -> int:
        return (
            self.adopt_local_recovery_artifacts()
            + self.reconcile_unaccounted_delivery_results()
            + self.cleanup_invalid_artifacts()
            + self.cleanup_orphan_input_artifacts()
        )

    def cleanup_orphan_input_artifacts(self, limit: int = 1000) -> int:
        repository = self._require_repository()
        if self.artifact_service is None:
            return 0
        retention_seconds = max(
            60,
            int(getattr(self.settings, "orphan_artifact_retention_seconds", 60 * 60)),
        )
        try:
            removed = self.artifact_service.cleanup_orphan_input_files(
                repository.list_input_artifact_paths(),
                older_than=datetime.now(timezone.utc) - timedelta(seconds=retention_seconds),
                limit=limit,
            )
        except Exception as exc:
            logger.warning({
                "event": "image_orphan_input_artifact_cleanup_failed",
                "error": str(exc),
            })
            return 0
        if removed:
            logger.info({
                "event": "image_orphan_input_artifacts_cleaned",
                "count": removed,
            })
        return removed

    def cleanup_invalid_artifacts(self, limit: int = 100) -> int:
        repository = self._require_repository()
        if self.artifact_service is None:
            return 0
        worker_id = _clean(getattr(self.worker, "worker_id", ""))
        cleaned = 0
        for artifact in repository.list_invalid_artifacts(
            limit=limit,
            worker_id=worker_id,
            include_unowned=not bool(worker_id),
        ):
            try:
                if not self.artifact_service.discard((artifact,)):
                    logger.warning({
                        "event": "image_invalid_artifact_cleanup_incomplete",
                        "relative_path": artifact.relative_path,
                    })
                    continue
                cleaned += repository.remove_invalid_artifacts(
                    artifact.task_id,
                    (artifact.relative_path,),
                )
            except Exception as exc:
                logger.warning({
                    "event": "image_invalid_artifact_cleanup_failed",
                    "relative_path": artifact.relative_path,
                    "error": str(exc),
                })
        return cleaned

    def _discard_artifacts_safely(
        self,
        artifacts: Sequence[ArtifactDescriptor],
        *,
        reason: str,
        claim: ClaimedJob | None = None,
    ) -> None:
        if not artifacts or self.artifact_service is None:
            return
        candidates = tuple(artifacts)
        repository = self.repository
        if repository is not None:
            if claim is not None:
                get_job = getattr(repository, "get_job", None)
                if callable(get_job):
                    try:
                        current_job = get_job(claim.job.id)
                    except Exception as exc:
                        logger.warning({
                            "event": "image_uncommitted_artifact_discard_skipped",
                            "reason": reason,
                            "job_id": str(claim.job.id),
                            "error": str(exc),
                        })
                        return
                    if (
                        current_job is None
                        or int(getattr(current_job, "lease_version", -1))
                        != int(claim.lease_version)
                        or current_job.status not in {JobStatus.LEASED, JobStatus.RUNNING}
                    ):
                        logger.info({
                            "event": "image_uncommitted_artifact_discard_fenced",
                            "reason": reason,
                            "job_id": str(claim.job.id),
                            "claim_lease_version": claim.lease_version,
                            "current_lease_version": (
                                getattr(current_job, "lease_version", None)
                                if current_job is not None
                                else None
                            ),
                            "current_status": (
                                getattr(getattr(current_job, "status", None), "value", "")
                                if current_job is not None
                                else ""
                            ),
                        })
                        return
            list_artifacts = getattr(repository, "list_artifacts", None)
            if callable(list_artifacts):
                try:
                    protected_paths = {
                        artifact.relative_path
                        for artifact in list_artifacts(candidates[0].task_id)
                        if artifact.status != ArtifactStatus.INVALID
                    }
                except Exception as exc:
                    if claim is not None:
                        logger.warning({
                            "event": "image_uncommitted_artifact_discard_skipped",
                            "reason": reason,
                            "task_id": str(candidates[0].task_id),
                            "error": str(exc),
                        })
                        return
                    protected_paths = set()
                candidates = tuple(
                    artifact
                    for artifact in candidates
                    if artifact.relative_path not in protected_paths
                )
        if not candidates:
            return
        try:
            discarded = self.artifact_service.discard(candidates)
        except Exception as exc:
            logger.error({
                "event": "image_uncommitted_artifact_discard_failed",
                "reason": reason,
                "artifacts": [item.relative_path for item in candidates],
                "error": str(exc),
            })
            return
        if not discarded:
            logger.error({
                "event": "image_uncommitted_artifact_discard_incomplete",
                "reason": reason,
                "artifacts": [item.relative_path for item in candidates],
            })

    def local_recovery_artifacts_available(
        self,
        job: JobSnapshot,
        artifacts: Sequence[ArtifactDescriptor],
    ) -> bool:
        if self.artifact_service is None:
            return False
        for artifact in artifacts:
            if artifact.kind not in {"downloaded", "upscaled", "final"}:
                continue
            if artifact.status != ArtifactStatus.READY:
                continue
            try:
                self.artifact_service.read(artifact)
            except Exception:
                continue
            return True
        return False

    def execute_claim(
        self,
        claim: ClaimedJob,
        access_token: str,
        runtime_guard: Callable[[], None] | None = None,
    ) -> None:
        repository = self._require_repository()
        if self.artifact_service is None:
            raise ImageQueueUnavailableError("image artifact service is not started")
        context = repository.get_execution_request(claim.job.task_id)
        if context is None:
            raise ValueError("claimed image task no longer exists")
        payload = dict(context["request_payload"])
        account_email = ""
        try:
            from services.account_service import account_service

            account = account_service.get_account(access_token)
            account_email = _clean((account or {}).get("email"))
        except Exception:
            account_email = ""
        worker_image_base_url = self._worker_image_base_url()
        delivery_base_url = worker_image_base_url or _clean(payload.get("base_url"))
        url_only_delivery = False
        artifact_history = repository.list_artifacts(claim.job.task_id)

        def preferred_artifact_sha256(kind: str) -> str:
            return next(
                (
                    item.sha256
                    for item in reversed(artifact_history)
                    if item.job_id == claim.job.id
                    and item.kind == kind
                    and item.status == ArtifactStatus.READY
                ),
                "",
            )

        conversation_id = ""
        managed_conversation_ids: set[str] = set()
        checkpointed_conversation_ids: set[str] = set()

        def track_conversation(value: object) -> str:
            conversation = _clean(value)
            if conversation:
                managed_conversation_ids.add(conversation)
            return conversation

        def track_error_conversations(error: BaseException) -> None:
            def collect(value: object) -> None:
                if isinstance(value, Mapping):
                    for key in ("conversation_id", "_conversation_id"):
                        track_conversation(value.get(key))
                    for nested in value.values():
                        if isinstance(nested, (Mapping, list, tuple)):
                            collect(nested)
                elif isinstance(value, (list, tuple)):
                    for nested in value:
                        collect(nested)

            track_conversation(getattr(error, "conversation_id", ""))
            collect(getattr(error, "last_conversation_snapshot", None))
            collect(getattr(error, "stream_timeout_followup", None))
            collect(getattr(error, "image_attempts", None))

        def cleanup_conversations(conversation_ids: set[str]) -> None:
            for item in sorted(conversation_ids):
                self._cleanup_conversation(access_token, item)

        def cleanup_failed_conversations() -> None:
            preserved = set(checkpointed_conversation_ids)
            if claim.job.conversation_id and (
                claim.job.image_urls
                or claim.job.file_ids
                or claim.job.sediment_ids
                or claim.job.stage in {
                    JobStage.RESOLVING,
                    JobStage.DOWNLOADING,
                    JobStage.TRANSFORMING,
                    JobStage.SAVING,
                }
            ):
                preserved.add(track_conversation(claim.job.conversation_id))
            cleanup_conversations(managed_conversation_ids - preserved)

        track_conversation(claim.job.conversation_id)
        final_artifact: ArtifactDescriptor | None = None
        result_payload: dict[str, Any] | None = None
        result_commit_started = bool(getattr(claim.job, "quota_consumed", False))

        uncommitted_artifacts: list[ArtifactDescriptor] = []

        def track_uncommitted_artifact(artifact: ArtifactDescriptor | None) -> ArtifactDescriptor | None:
            if artifact is not None:
                uncommitted_artifacts.append(artifact)
            return artifact

        def mark_uncommitted_artifact_committed(artifact: ArtifactDescriptor | None) -> None:
            if artifact is None:
                return
            for index, item in enumerate(list(uncommitted_artifacts)):
                if item.relative_path == artifact.relative_path:
                    del uncommitted_artifacts[index]
                    return

        def artifact_already_recorded(artifact: ArtifactDescriptor | None) -> bool:
            if artifact is None:
                return False
            return any(
                item.relative_path == artifact.relative_path
                and item.kind == artifact.kind
                and item.status == ArtifactStatus.READY
                for item in artifact_history
            )

        def track_recovered_uncommitted_artifact(
            artifact: ArtifactDescriptor | None,
        ) -> ArtifactDescriptor | None:
            if artifact is not None and not artifact_already_recorded(artifact):
                track_uncommitted_artifact(artifact)
            return artifact

        try:
            claim_deadline = float(getattr(runtime_guard, "deadline_monotonic", 0.0) or 0.0)

            def ensure_active() -> None:
                if result_commit_started:
                    return
                if repository.is_cancel_requested(claim.job.task_id):
                    raise RuntimeError("image task was canceled")
                if runtime_guard is not None:
                    runtime_guard()

            def note_stage(stage: JobStage) -> None:
                callback = getattr(self.worker, "note_claim_stage", None)
                if callable(callback):
                    callback(claim, stage)

            def record_stage(image_data: bytes, kind: str, source_url: str = "") -> ArtifactDescriptor:
                ensure_active()
                note_stage(JobStage.DOWNLOADING if kind == "downloaded" else JobStage.TRANSFORMING)
                descriptor = track_uncommitted_artifact(self._run_in_worker_pool(
                    "io",
                    lambda: self.artifact_service.persist_stage(
                        claim.job.task_id,
                        claim.job.id,
                        image_data,
                        kind,
                        source_url=source_url,
                    ),
                ))
                assert descriptor is not None
                ensure_active()
                if not repository.record_artifact(claim, descriptor):
                    raise RuntimeError("image job lease was lost while recording an artifact")
                mark_uncommitted_artifact_committed(descriptor)
                return descriptor

            if claim.job.stage == JobStage.SAVING:
                ensure_active()
                source_urls = list(claim.job.image_urls)
                recovered_artifact = track_recovered_uncommitted_artifact(self._run_in_worker_pool(
                    "io",
                    lambda: self.artifact_service.recover_final(
                        claim.job.task_id,
                        claim.job.id,
                        delivery_base_url,
                        source_url=_clean(source_urls[0]) if source_urls else "",
                        preferred_sha256=preferred_artifact_sha256("final"),
                        not_after=claim.job.available_at,
                    ),
                ))
                ensure_active()
                if recovered_artifact is not None:
                    result_commit_started = True
                    recovered_result = self._delivery_result_payload(
                        recovered_artifact,
                        context,
                        delivery_base_url=delivery_base_url,
                        original_url=_clean(source_urls[0]) if source_urls else "",
                        url_only_delivery=url_only_delivery,
                        account_email=account_email,
                    )
                    completed = repository.complete_job(claim, recovered_artifact, recovered_result)
                    if completed is None:
                        raise RuntimeError("image job lease was lost before recovered result commit")
                    mark_uncommitted_artifact_committed(recovered_artifact)
                    cleanup_conversations(managed_conversation_ids)
                    return

            def checkpoint(value: JobCheckpoint) -> None:
                nonlocal conversation_id
                ensure_active()
                note_stage(value.stage)
                if value.conversation_id:
                    conversation_id = track_conversation(value.conversation_id)
                if not repository.checkpoint_job(claim, value):
                    raise RuntimeError("image job lease was lost while checkpointing")
                if value.conversation_id:
                    checkpointed_conversation_ids.add(track_conversation(value.conversation_id))

            def format_result(image_data: bytes, details: Mapping[str, Any]) -> dict[str, Any]:
                nonlocal final_artifact, result_payload, conversation_id, result_commit_started
                ensure_active()
                if final_artifact is not None:
                    raise RuntimeError("single image job returned multiple final images")
                if not repository.mark_quota_consumed(claim):
                    raise RuntimeError("image job lease was lost while recording quota consumption")
                result_commit_started = True
                detail_conversation_id = track_conversation(details.get("conversation_id"))
                if detail_conversation_id:
                    conversation_id = detail_conversation_id
                source_urls = list(details.get("image_urls") or [])
                downloaded_artifact = record_stage(
                    image_data,
                    "downloaded",
                    _clean(source_urls[0]) if source_urls else "",
                )
                checkpoint(JobCheckpoint(
                    stage=JobStage.TRANSFORMING,
                    conversation_id=conversation_id,
                    image_urls=source_urls,
                ))
                upscale_outcome = self._run_in_worker_pool(
                    "upscale",
                    lambda: self.upscaler(self.artifact_service.read(downloaded_artifact), payload.get("size")),
                )
                ensure_active()
                final_bytes = getattr(upscale_outcome, "payload", upscale_outcome)
                upscale_event = _clean(getattr(upscale_outcome, "event_type", ""))
                if upscale_event and not repository.record_claim_event(
                    claim,
                    upscale_event,
                    dict(getattr(upscale_outcome, "event_data", {}) or {}),
                ):
                    raise RuntimeError("image job lease was lost while recording upscale fallback")
                upscaled_artifact = record_stage(
                    final_bytes,
                    "upscaled",
                    _clean(source_urls[0]) if source_urls else "",
                )
                checkpoint(JobCheckpoint(
                    stage=JobStage.SAVING,
                    conversation_id=conversation_id,
                    image_urls=source_urls,
                ))
                final_artifact = track_uncommitted_artifact(self._run_in_worker_pool(
                    # The generation slot was released by the SAVING checkpoint above.
                    "io",
                    lambda: self.artifact_service.persist_final(
                        claim.job.task_id,
                        claim.job.id,
                        self.artifact_service.read(upscaled_artifact),
                        delivery_base_url,
                        source_url=_clean(source_urls[0]) if source_urls else "",
                    ),
                ))
                assert final_artifact is not None
                ensure_active()
                result_payload = self._delivery_result_payload(
                    final_artifact,
                    context,
                    delivery_base_url=delivery_base_url,
                    original_url=_clean(source_urls[0]) if source_urls else "",
                    url_only_delivery=url_only_delivery,
                    account_email=account_email,
                )
                return dict(result_payload)

            from services.protocol.conversation import ConversationRequest, encode_images

            encoded_images: list[str] | None = None
            ensure_active()
            if context["task_type"] == "edit":
                inputs = self._read_input_artifacts(claim.job.task_id, "input")
                masks = self._read_input_artifacts(claim.job.task_id, "mask")
                if masks:
                    from services.protocol.openai_v1_image_edit import _composite_mask

                    inputs = _composite_mask(inputs, masks)
                encoded_images = encode_images(inputs)
                if not encoded_images:
                    raise ValueError("image edit task has no persisted input artifact")
            outputs: list[Any] = []
            recovered_upscaled = track_recovered_uncommitted_artifact(self._run_in_worker_pool(
                "io",
                lambda: self.artifact_service.recover_stage(
                    claim.job.task_id,
                    claim.job.id,
                    "upscaled",
                    preferred_sha256=preferred_artifact_sha256("upscaled"),
                    not_after=claim.job.available_at,
                ),
            ))
            ensure_active()
            if recovered_upscaled is not None:
                ensure_active()
                if not repository.record_artifact(claim, recovered_upscaled):
                    raise RuntimeError("image job lease was lost while recovering an upscaled artifact")
                mark_uncommitted_artifact_committed(recovered_upscaled)
                source_urls = list(claim.job.image_urls)
                checkpoint(JobCheckpoint(
                    stage=JobStage.SAVING,
                    conversation_id=claim.job.conversation_id,
                    image_urls=source_urls,
                    file_ids=list(claim.job.file_ids),
                    sediment_ids=list(claim.job.sediment_ids),
                ))
                final_artifact = track_uncommitted_artifact(self._run_in_worker_pool(
                    "io",
                    lambda: self.artifact_service.persist_final(
                        claim.job.task_id,
                        claim.job.id,
                        self.artifact_service.read(recovered_upscaled),
                        delivery_base_url,
                        source_url=_clean(source_urls[0]) if source_urls else "",
                    ),
                ))
                assert final_artifact is not None
                ensure_active()
                result_payload = self._delivery_result_payload(
                    final_artifact,
                    context,
                    delivery_base_url=delivery_base_url,
                    original_url=_clean(source_urls[0]) if source_urls else "",
                    url_only_delivery=url_only_delivery,
                    account_email=account_email,
                )

            recovered_downloaded = None
            if final_artifact is None:
                recovered_downloaded = track_recovered_uncommitted_artifact(self._run_in_worker_pool(
                    "io",
                    lambda: self.artifact_service.recover_stage(
                        claim.job.task_id,
                        claim.job.id,
                        "downloaded",
                        preferred_sha256=preferred_artifact_sha256("downloaded"),
                        not_after=claim.job.available_at,
                    ),
                ))
                ensure_active()
                if recovered_downloaded is not None and not repository.record_artifact(claim, recovered_downloaded):
                    raise RuntimeError("image job lease was lost while recovering a downloaded artifact")
                mark_uncommitted_artifact_committed(recovered_downloaded)

            checkpoint_urls = list(claim.job.image_urls)
            downloaded = [self.artifact_service.read(recovered_downloaded)] if recovered_downloaded is not None else []

            if (
                claim.account_slot < 0
                and recovered_upscaled is None
                and recovered_downloaded is None
                and repository.invalidate_recovery_artifacts(claim) > 0
            ):
                raise LocalArtifactRecoveryUnavailable(
                    "local recovery artifacts are unavailable"
                )

            def resolve_checkpoint():
                ensure_active()
                with self._build_backend(access_token, claim_deadline) as backend:
                    file_ids = list(claim.job.file_ids)
                    sediment_ids = list(claim.job.sediment_ids)
                    if not file_ids and not sediment_ids:
                        file_ids, sediment_ids = backend._poll_image_results(
                            claim.job.conversation_id,
                            _recovery_poll_timeout(payload),
                            cancel_callback=ensure_active,
                        )
                    urls = backend.resolve_conversation_image_urls(
                        claim.job.conversation_id,
                        file_ids,
                        sediment_ids,
                        poll=False,
                        cancel_callback=ensure_active,
                    )
                    ensure_active()
                    return file_ids, sediment_ids, urls

            if final_artifact is not None:
                pass
            elif recovered_downloaded is not None:
                pass
            elif not checkpoint_urls and claim.job.conversation_id:
                checkpoint(JobCheckpoint(
                    stage=JobStage.RESOLVING,
                    conversation_id=claim.job.conversation_id,
                    file_ids=list(claim.job.file_ids),
                    sediment_ids=list(claim.job.sediment_ids),
                ))
                file_ids, sediment_ids, checkpoint_urls = self._run_in_worker_pool("io", resolve_checkpoint)
                checkpoint(JobCheckpoint(
                    stage=JobStage.DOWNLOADING,
                    conversation_id=claim.job.conversation_id,
                    image_urls=list(checkpoint_urls),
                    file_ids=file_ids,
                    sediment_ids=sediment_ids,
                ))
                downloaded = self._run_in_worker_pool(
                    "io",
                    lambda: self._download_image_urls(
                        access_token,
                        checkpoint_urls,
                        deadline_monotonic=claim_deadline,
                        cancel_callback=ensure_active,
                    ),
                )
            elif checkpoint_urls:
                checkpoint(JobCheckpoint(
                    stage=JobStage.DOWNLOADING,
                    conversation_id=claim.job.conversation_id,
                    image_urls=checkpoint_urls,
                    file_ids=list(claim.job.file_ids),
                    sediment_ids=list(claim.job.sediment_ids),
                ))
                try:
                    downloaded = self._run_in_worker_pool(
                        "io",
                        lambda: self._download_image_urls(
                            access_token,
                            checkpoint_urls,
                            deadline_monotonic=claim_deadline,
                            cancel_callback=ensure_active,
                        ),
                    )
                except Exception:
                    if not claim.job.conversation_id:
                        raise
                    file_ids, sediment_ids, checkpoint_urls = self._run_in_worker_pool("io", resolve_checkpoint)
                    checkpoint(JobCheckpoint(
                        stage=JobStage.DOWNLOADING,
                        conversation_id=claim.job.conversation_id,
                        image_urls=list(checkpoint_urls),
                        file_ids=file_ids,
                        sediment_ids=sediment_ids,
                    ))
                    downloaded = self._run_in_worker_pool(
                        "io",
                        lambda: self._download_image_urls(
                            access_token,
                            checkpoint_urls,
                            deadline_monotonic=claim_deadline,
                            cancel_callback=ensure_active,
                        ),
                    )
            else:
                downloaded = []

            if final_artifact is not None:
                pass
            elif checkpoint_urls or recovered_downloaded is not None:
                if len(downloaded) != 1:
                    raise RuntimeError("single image job recovery returned an unexpected image count")
                format_result(downloaded[0], {
                    "conversation_id": claim.job.conversation_id,
                    "image_urls": checkpoint_urls,
                })
            else:
                checkpoint(JobCheckpoint(stage=JobStage.GENERATING))
                outputs = self.job_generator(ConversationRequest(
                    model=_clean(context.get("public_model"), "gpt-image-2"),
                    prompt=_clean(context.get("effective_prompt")),
                    images=encoded_images,
                    n=1,
                    size=payload.get("size"),
                    quality=_clean(payload.get("quality"), "auto"),
                    response_format="url",
                    base_url=delivery_base_url or None,
                    message_as_error=True,
                    deadline_monotonic=claim_deadline,
                    managed_access_token=access_token,
                    managed_account_id=_clean(claim.account_id),
                    checkpoint_callback=checkpoint,
                    cancel_requested_callback=ensure_active,
                    image_result_formatter=format_result,
                    defer_conversation_cleanup=True,
                    durable_context={
                        "task_id": str(claim.job.task_id),
                        "job_id": str(claim.job.id),
                        "trace_headers": dict(payload.get("trace_headers") or {}),
                    },
                ))
            ensure_active()
            if final_artifact is None or result_payload is None:
                raise RuntimeError("image generation completed without a saved artifact")
            for output in outputs or []:
                output_conversation_id = track_conversation(getattr(output, "conversation_id", ""))
                if output_conversation_id:
                    conversation_id = output_conversation_id
            ensure_active()
            completed = repository.complete_job(claim, final_artifact, result_payload)
            if completed is None:
                raise RuntimeError("image job lease was lost before result commit")
            mark_uncommitted_artifact_committed(final_artifact)
            cleanup_conversations(managed_conversation_ids)

        except Exception as exc:
            track_error_conversations(exc)
            self._discard_artifacts_safely(
                uncommitted_artifacts,
                reason="image_claim_uncommitted_artifacts",
                claim=claim,
            )
            cleanup_failed_conversations()
            raise

    def _cleanup_conversation(self, access_token: str, conversation_id: str) -> None:
        try:
            self._run_in_worker_pool(
                "io",
                lambda: self.conversation_cleanup(access_token, conversation_id),
            )
        except Exception as exc:
            logger.error({
                "event": "image_conversation_cleanup_failed",
                "conversation_id": conversation_id,
                "error": str(exc),
            })

    def _download_image_urls(
        self,
        access_token: str,
        urls: list[str],
        *,
        deadline_monotonic: float = 0.0,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[bytes]:
        with self._build_backend(access_token, deadline_monotonic) as backend:
            if callable(cancel_callback):
                return backend.download_image_bytes(
                    urls,
                    cancel_callback=cancel_callback,
                )
            return backend.download_image_bytes(urls)

    def _find_existing_idempotent_task(
        self,
        repository: ImageQueueRepository,
        owner: str,
        client_id: str,
        selected_key: str,
    ) -> TaskSnapshot | None:
        existing_by_key = repository.get_task_by_idempotency_key(owner, selected_key) if selected_key else None
        existing_by_client = repository.get_task_by_client_id(owner, client_id) if client_id else None
        if (
            existing_by_key is not None
            and existing_by_client is not None
            and existing_by_key.id != existing_by_client.id
        ):
            raise IdempotencyConflict("idempotency key and client_task_id refer to different image tasks")
        return existing_by_key or existing_by_client

    def _replayable_existing_task(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        idempotency_key: str,
        task_type: str,
        source_request_hash: str,
    ) -> TaskSnapshot | None:
        source_hash = _clean(source_request_hash)
        if not source_hash:
            return None
        repository = self._require_repository()
        owner = _owner_key(identity)
        client_id = _clean_client_task_id(client_task_id)
        selected_key = select_idempotency_key({}, idempotency_key or client_id)
        existing = self._find_existing_idempotent_task(repository, owner, client_id, selected_key)
        if existing is None:
            return None
        if _clean(existing.task_type, "generation") != task_type:
            raise IdempotencyConflict("idempotency key was already used with a different image task type")
        stored_hash = _clean(existing.request_payload.get("source_request_hash"))
        if not stored_hash:
            return None
        if stored_hash != source_hash:
            raise IdempotencyConflict("idempotency key was already used with a different request")
        return self._ensure_deliverable_task(owner, existing, force_url_verification=True)

    def replay_existing_edit_task(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        idempotency_key: str,
        source_request_hash: str,
    ) -> dict[str, Any] | None:
        existing = self._replayable_existing_task(
            identity,
            client_task_id=client_task_id,
            idempotency_key=idempotency_key,
            task_type="edit",
            source_request_hash=source_request_hash,
        )
        return self._public_snapshot(existing) if existing is not None else None

    def replay_existing_protocol_submission(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        idempotency_key: str,
        task_type: str,
        source_request_hash: str,
    ) -> dict[str, Any] | None:
        existing = self._replayable_existing_task(
            identity,
            client_task_id=client_task_id,
            idempotency_key=idempotency_key,
            task_type=task_type,
            source_request_hash=source_request_hash,
        )
        if existing is None:
            return None
        return {
            "identity": dict(identity),
            "request_payload": dict(existing.request_payload or {}),
            "task_id": str(existing.id),
            "task_type": str(existing.task_type or ""),
            "response_format": _clean(existing.request_payload.get("response_format")),
        }

    def _submit(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        mode: str,
        prompt: str,
        model: str,
        n: int,
        size: str | None,
        quality: str,
        base_url: str,
        idempotency_key: str = "",
        trace_headers: Mapping[str, object] | None = None,
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
        response_format: str = "url",
        source_request_hash: str = "",
        source_endpoint: str = "",
        request_started_at: float | None = None,
    ) -> dict[str, Any]:
        repository = self._require_repository()
        public_model = require_public_image_model(model)
        client_id = _clean_client_task_id(client_task_id)
        selected_key = select_idempotency_key({}, idempotency_key or client_id)
        if not selected_key:
            raise ValueError("idempotency key or client_task_id is required")
        original_prompt = _clean(prompt)
        if not original_prompt:
            raise ValueError("prompt is required")
        effective_prompt, suffix_version = build_effective_prompt(original_prompt, self.settings)
        count = _image_count(n)
        legacy_hash_payload = {
            "mode": mode,
            "prompt": original_prompt,
            "model": public_model,
            "n": count,
            "size": size,
            "quality": quality,
            "response_format": response_format,
            "images": images or [],
            "masks": masks or [],
        }
        hash_payload = {
            **legacy_hash_payload,
            "prompt": effective_prompt,
            "prompt_suffix_version": suffix_version or "",
        }
        request_hash = canonical_request_hash(hash_payload)
        legacy_request_hash = canonical_request_hash(legacy_hash_payload)
        owner = _owner_key(identity)
        existing = self._find_existing_idempotent_task(repository, owner, client_id, selected_key)
        if existing is not None and existing.request_hash != request_hash:
            same_effective_prompt = _clean(existing.request_payload.get("prompt")) == effective_prompt
            if existing.request_hash != legacy_request_hash or not same_effective_prompt:
                raise IdempotencyConflict("idempotency key was already used with a different request")
        if existing is not None:
            existing = self._ensure_deliverable_task(owner, existing, force_url_verification=True)
            return self._public_snapshot(existing)
        self._ensure_backlog_capacity(repository)
        self._ensure_submission_capacity()
        task_id = existing.id if existing is not None else uuid4()
        image_paths, mask_paths, input_artifacts = self._persist_inputs(task_id, images, masks)
        payload: dict[str, Any] = {
            "prompt": effective_prompt,
            "model": public_model,
            "n": count,
            "size": size,
            "quality": _clean(quality, "auto"),
            "response_format": response_format,
            "base_url": _clean(base_url),
            "trace_headers": sanitize_trace_headers(trace_headers or {}),
        }
        trace_payload = dict(payload["trace_headers"])
        if trace_payload.get("call_id"):
            payload["call_id"] = trace_payload["call_id"]
        source_hash = _clean(source_request_hash)
        if source_hash:
            payload["source_request_hash"] = source_hash
        endpoint = _clean(source_endpoint)
        if endpoint:
            payload["source_endpoint"] = endpoint[:255]
        try:
            started_at = float(request_started_at or 0.0)
        except (TypeError, ValueError):
            started_at = 0.0
        if started_at > 0:
            payload["request_started_at"] = started_at
        if image_paths:
            payload["input_artifacts"] = image_paths
        if mask_paths:
            payload["mask_artifacts"] = mask_paths
        request = EnqueueRequest(
            owner_key=owner,
            idempotency_key=selected_key,
            request_hash=request_hash,
            task_type="edit" if mode == "edit" else "generation",
            original_prompt=original_prompt,
            effective_prompt=effective_prompt,
            request_payload=payload,
            required_jobs=count,
            client_task_id=client_id,
            public_model=public_model,
            prompt_suffix_version=suffix_version,
            task_id=task_id,
            input_artifacts=input_artifacts,
        )
        try:
            if self._enqueue_accepts_max_backlog(repository):
                result = repository.enqueue_task(request, max_backlog=self.settings.max_backlog)
            else:
                result = repository.enqueue_task(request)
        except Exception:
            self.artifact_service.discard(input_artifacts)
            raise
        if not result.created:
            self.artifact_service.discard(input_artifacts)
        if result.created and self.worker is not None:
            self.worker.notify()
        return self._public_snapshot(result.task)

    def submit_generation(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        n: int = 1,
        size: str | None = None,
        quality: str = "auto",
        base_url: str = "",
        idempotency_key: str = "",
        trace_headers: Mapping[str, object] | None = None,
        response_format: str = "url",
        source_endpoint: str = "",
        request_started_at: float | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            identity,
            client_task_id=client_task_id,
            mode="generate",
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            base_url=base_url,
            idempotency_key=idempotency_key,
            trace_headers=trace_headers,
            response_format=response_format,
            source_endpoint=source_endpoint,
            request_started_at=request_started_at,
        )

    def submit_edit(
        self,
        identity: Mapping[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        n: int = 1,
        size: str | None = None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
        idempotency_key: str = "",
        trace_headers: Mapping[str, object] | None = None,
        response_format: str = "url",
        source_request_hash: str = "",
        source_endpoint: str = "",
        request_started_at: float | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            identity,
            client_task_id=client_task_id,
            mode="edit",
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            base_url=base_url,
            idempotency_key=idempotency_key,
            trace_headers=trace_headers,
            images=images,
            masks=masks,
            response_format=response_format,
            source_request_hash=source_request_hash,
            source_endpoint=source_endpoint,
            request_started_at=request_started_at,
        )

    def progress_snapshot(
        self,
        identity: Mapping[str, object] | str,
        task_id: object,
    ) -> dict[str, Any]:
        """读取任务进度字段，不做交付校验，用于流式心跳。

        与 get_task 不同，这里不触发 artifact 校验和 URL 复核，因为心跳只需要
        排队位置和阶段文案，任务尚未进入终态。
        """
        task = self._require_repository().get_task(_owner_key(identity), task_id)
        if task is None:
            return {}
        return self._public_snapshot(task)

    def get_task(self, identity: Mapping[str, object] | str, task_id: object) -> dict[str, Any]:
        repository = self._require_repository()
        owner = _owner_key(identity)
        task = repository.get_task(owner, task_id)
        if task is None:
            raise ValueError(
                "image task not found; if multiple gptimage2api instances share a "
                "load balancer without sticky sessions, use shared PostgreSQL and "
                "node-owned image URL delivery, or enable request affinity"
            )
        task = self._ensure_deliverable_task(
            owner,
            task,
        )
        task = self._mark_response_attempted_for_deliverable_result(owner, task)
        return self._public_snapshot(task)

    def list_tasks(
        self,
        identity: Mapping[str, object],
        task_ids: list[str],
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        repository = self._require_repository()
        owner = _owner_key(identity)
        requested = [item for item in (_clean(value) for value in task_ids) if item]
        snapshots = repository.list_tasks(owner, requested or None, limit=limit, offset=offset)
        snapshots = [
            self._ensure_deliverable_task(
                owner,
                item,
            )
            for item in snapshots
        ]
        snapshots = [
            self._mark_response_attempted_for_deliverable_result(owner, item)
            for item in snapshots
        ]
        active_ids = [item.id for item in snapshots if item.status not in TERMINAL_TASK_STATUSES]
        positions, worker_pause_reason = repository.queue_context(active_ids)
        found = (
            {str(item.id) for item in snapshots}
            | {item.client_task_id for item in snapshots if item.client_task_id}
            | {item.idempotency_key for item in snapshots if item.idempotency_key}
        )
        return {
            "items": [
                self._public_snapshot(item, positions.get(item.id, 0), worker_pause_reason)
                for item in snapshots
            ],
            "missing_ids": [item for item in requested if item not in found],
            "limit": min(500, max(1, int(limit))),
            "offset": max(0, int(offset)),
        }

    def list_public_final_artifacts(self) -> list[dict[str, Any]]:
        return self._require_repository().list_public_final_artifacts()

    def is_public_final_artifact(self, relative_path: object) -> bool:
        return self._require_repository().is_public_final_artifact(relative_path)

    def delete_public_final_artifact(self, relative_path: object) -> bool:
        rel = _clean(relative_path).replace("\\", "/").lstrip("/")
        if not rel:
            return False
        repository = self._require_repository()
        descriptor = repository.public_final_artifact_descriptor(rel)
        if descriptor is None or self.artifact_service is None:
            return False
        if not self.artifact_service.discard((descriptor,)):
            return False
        invalidated = repository.invalidate_public_final_artifact(rel)
        if invalidated is None:
            return False
        return bool(repository.remove_invalid_artifacts(
            invalidated.task_id,
            (invalidated.relative_path,),
        ))

    @staticmethod
    def _is_waitable_terminal(task: TaskSnapshot) -> bool:
        if task.status == TaskStatus.SUCCESS:
            return task.succeeded_jobs >= task.required_jobs
        if task.status == TaskStatus.CANCELED:
            return True
        if task.status == TaskStatus.FAILED:
            # Wait until sibling jobs finish so partial artifacts are stable.
            return (
                task.completed_at is not None
                or task.succeeded_jobs + task.failed_jobs >= max(1, task.required_jobs)
            )
        return False

    def wait_for_terminal(
        self,
        owner: Mapping[str, object] | str,
        task_id: object,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        owner_key = _owner_key(owner)
        while True:
            task = self._require_repository().get_task(owner_key, task_id)
            if task is None:
                raise ValueError("image task not found")
            if self._is_waitable_terminal(task):
                task = self._ensure_deliverable_task(owner_key, task, force_url_verification=True)
                if self._is_waitable_terminal(task):
                    task = self._mark_response_attempted_for_deliverable_result(owner_key, task)
                    return self._public_snapshot(task)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("image task is still running")
            time.sleep(self.settings.result_wait_poll_seconds)

    async def wait_for_terminal_async(
        self,
        owner: Mapping[str, object] | str,
        task_id: object,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        repository = self._require_repository()
        owner_key = _owner_key(owner)
        key = str(task_id or "")
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._waiters_lock:
            self._terminal_waiters.setdefault(key, set()).add(waiter)
        try:
            while True:
                event.clear()
                task = await asyncio.to_thread(repository.get_task, owner_key, task_id)
                if task is None:
                    raise ValueError("image task not found")
                if self._is_waitable_terminal(task):
                    task = await asyncio.to_thread(
                        self._ensure_deliverable_task,
                        owner_key,
                        task,
                        force_url_verification=True,
                    )
                    if self._is_waitable_terminal(task):
                        task = await asyncio.to_thread(
                            self._mark_response_attempted_for_deliverable_result,
                            owner_key,
                            task,
                        )
                        return self._public_snapshot(task)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("image task is still running")
                fallback_poll = max(1.0, min(5.0, self.settings.result_wait_poll_seconds * 20.0))
                wait_timeout = fallback_poll if remaining is None else min(fallback_poll, remaining)
                try:
                    await asyncio.wait_for(event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue
        finally:
            with self._waiters_lock:
                waiters = self._terminal_waiters.get(key)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        self._terminal_waiters.pop(key, None)

    def cancel(self, identity: Mapping[str, object], task_id: object) -> dict[str, Any]:
        task = self._require_repository().request_cancel(_owner_key(identity), task_id)
        if task is None:
            raise ValueError("image task not found")
        self._handle_task_state_change(task.id)
        return self._public_snapshot(task)

    def acknowledge(self, identity: Mapping[str, object], task_id: object) -> dict[str, Any]:
        repository = self._require_repository()
        owner = _owner_key(identity)
        current = repository.get_task(owner, task_id)
        if current is None:
            raise ValueError("image task not found")
        current = self._ensure_deliverable_task(
            owner,
            current,
        )
        task = repository.acknowledge(owner, task_id)
        if task is None:
            raise ValueError("image task not found")
        self._handle_task_state_change(task.id)
        return self._public_snapshot(task)

    def mark_response_attempted(self, identity: Mapping[str, object], task_id: object) -> dict[str, Any]:
        task = self._require_repository().mark_response_attempted(_owner_key(identity), task_id)
        if task is None:
            raise ValueError("image task not found")
        return self._public_snapshot(task)

    def read_result_artifact(
        self,
        identity: Mapping[str, object],
        task_id: object,
        relative_path: str,
    ) -> bytes:
        task = self._require_repository().get_task(_owner_key(identity), task_id)
        if task is None:
            raise ValueError("image task not found")
        if self.artifact_service is None:
            raise ImageQueueUnavailableError("image artifact service is not started")
        artifact = next(
            (
                item
                for item in self._require_repository().list_artifacts(task.id)
                if (
                    item.kind == "final"
                    and item.status == ArtifactStatus.READY
                    and item.relative_path == relative_path
                )
            ),
            None,
        )
        if artifact is None:
            raise InvalidImageArtifact("image result artifact not found")
        root = self.artifact_service.root.resolve()
        path = (root / artifact.relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise InvalidImageArtifact("image result artifact path escapes storage root") from exc
        storage_service = getattr(self.artifact_service, "storage_service", None)
        remote_reader = getattr(storage_service, "get_remote_bytes", None) if storage_service is not None else None
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_artifact_bytes", None)
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_published_bytes", None)
        if not callable(remote_reader) and storage_service is not None:
            remote_reader = getattr(storage_service, "get_bytes", None)

        def verify_payload(payload: bytes) -> bytes:
            try:
                verified = verify_image_bytes(payload)
            except ValueError as exc:
                raise InvalidImageArtifact("image result artifact is unreadable") from exc
            if not payload or len(payload) != artifact.byte_size:
                raise InvalidImageArtifact("image result artifact size mismatch")
            if sha256(payload).hexdigest() != artifact.sha256:
                raise InvalidImageArtifact("image result artifact checksum mismatch")
            if (verified.width, verified.height) != (artifact.width, artifact.height):
                raise InvalidImageArtifact("image result artifact dimensions mismatch")
            return payload

        def read_verified() -> bytes:
            try:
                return self.artifact_service.read(artifact)
            except InvalidImageArtifact:
                if not callable(remote_reader):
                    raise
            except OSError:
                if not callable(remote_reader):
                    raise InvalidImageArtifact("image result artifact is unreadable")
            if callable(remote_reader):
                try:
                    return verify_payload(remote_reader(artifact.relative_path))
                except InvalidImageArtifact:
                    raise
                except Exception as exc:
                    raise InvalidImageArtifact("image result artifact is unreadable") from exc
            raise InvalidImageArtifact("image result artifact is unreadable")

        return self._run_in_worker_pool("io", read_verified)

    def verify_public_image_route(self, relative_path: str) -> None:
        """Verify the same handler used by ``GET /images/{path}``."""

        try:
            from fastapi.responses import FileResponse, Response
            from services.image_service import get_image_response

            response = get_image_response(relative_path)
            if isinstance(response, FileResponse):
                path = Path(str(getattr(response, "path", "") or ""))
                if not path.is_file():
                    raise InvalidImageArtifact("public image route returned a missing file")
                return
            if isinstance(response, Response):
                body = getattr(response, "body", None)
                if not isinstance(body, (bytes, bytearray)) or not body:
                    raise InvalidImageArtifact("public image route returned an empty body")
                return
            raise InvalidImageArtifact("public image route returned an invalid response")
        except InvalidImageArtifact:
            raise
        except Exception as exc:
            raise InvalidImageArtifact("public image route is unavailable") from exc

    def resume_poll(
        self,
        identity: Mapping[str, object],
        task_id: object,
        extra_timeout_secs: float | None = None,
    ) -> dict[str, Any]:
        timeout = _recovery_poll_timeout(
            {"resume_poll_timeout_seconds": extra_timeout_secs}
            if extra_timeout_secs is not None
            else {}
        )
        task = self._require_repository().resume_failed_task(
            _owner_key(identity),
            task_id,
            timeout,
        )
        if task is None:
            raise ValueError("image task not found")
        if self.worker is not None:
            self.worker.notify()
        task = self._mark_response_attempted_for_deliverable_result(_owner_key(identity), task)
        return self._public_snapshot(task)

    def submit_protocol_request(
        self,
        identity: Mapping[str, object],
        payload: Mapping[str, Any],
        mode: str,
        idempotency_key: str,
        trace_headers: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        client_task_id = _clean_client_task_id(payload.get("client_task_id") or idempotency_key or uuid4())
        common = dict(
            client_task_id=client_task_id,
            prompt=_clean(payload.get("prompt")),
            model=_clean(payload.get("model"), "gpt-image-2"),
            n=_image_count(payload.get("n")),
            size=payload.get("size"),
            quality=_clean(payload.get("quality"), "auto"),
            base_url=self._delivery_base_url(payload),
            idempotency_key=idempotency_key,
            trace_headers=trace_headers,
            response_format=_clean(payload.get("response_format"), "b64_json"),
            source_endpoint=_clean(payload.get("source_endpoint")),
            request_started_at=payload.get("request_started_at"),
        )
        if mode == "edit":
            return self.submit_edit(
                identity,
                images=list(payload.get("images") or []),
                masks=list(payload.get("mask") or []),
                source_request_hash=_clean(payload.get("source_request_hash")),
                **common,
            )
        return self.submit_generation(identity, **common)


image_task_service = ImageTaskService()
