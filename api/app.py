from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event, RLock, Thread
from typing import Callable, Sequence

from anyio.to_thread import current_default_thread_limiter
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_tasks, prompts, register, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, start_account_lifecycle_watcher
from services.account_service import account_service
from services.backup_service import backup_service
from services.config import config
from services.dashboard_metrics_service import dashboard_metrics_service
from services.genbox_push_service import (
    shutdown_genbox_push_service,
    start_genbox_push_service,
)
from services.image_task_service import image_task_service
from services.log_service import log_service
from services.realtime_monitor_service import realtime_monitor_service
from services.register_service import register_service
from services.retention_cleanup_service import retention_cleanup_coordinator, start_retention_cleanup_scheduler
from services.runtime_configuration import DEFAULT_THREAD_TOKENS, env_value
from utils.log import logger


RETENTION_SHUTDOWN_TIMEOUT_SECS = 1.0
BACKUP_RESTORE_THREAD_JOIN_TIMEOUT_SECS = 30.0


class _BackupRestoreMaintenance:
    """Own the process-wide quiesce boundary used by backup restoration."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._stop_event: Event | None = None
        self._threads: tuple[Thread, ...] = ()
        self._service_stops: tuple[tuple[str, Callable[[], object]], ...] = ()
        self._stopped = False
        self._stop_error = ""

    def activate(
        self,
        *,
        stop_event: Event,
        threads: Sequence[Thread | None],
        service_stops: Sequence[tuple[str, Callable[[], object]]],
    ) -> None:
        with self._lock:
            self._stop_event = stop_event
            self._threads = tuple(thread for thread in threads if thread is not None)
            self._service_stops = tuple(service_stops)
            self._stopped = False
            self._stop_error = ""

    def deactivate(self) -> None:
        with self._lock:
            self._stop_event = None
            self._threads = ()
            self._service_stops = ()
            self._stopped = False
            self._stop_error = ""

    def stop(self) -> None:
        with self._lock:
            if self._stop_error:
                raise RuntimeError(self._stop_error)
            if self._stopped:
                return
            self._stopped = True
            stop_event = self._stop_event
            threads = self._threads
            service_stops = self._service_stops

        if stop_event is not None:
            stop_event.set()

        failures: list[str] = []
        for name, stop in service_stops:
            try:
                stop()
            except Exception as exc:
                failures.append(f"{name} 停止失败：{exc}")

        for thread in threads:
            try:
                thread.join(BACKUP_RESTORE_THREAD_JOIN_TIMEOUT_SECS)
                if thread.is_alive():
                    failures.append(f"{thread.name or 'unknown'} 线程未停止")
            except Exception as exc:
                failures.append(f"{thread.name or 'unknown'} 线程停止失败：{exc}")

        if failures:
            message = "；".join(failures)
            with self._lock:
                self._stop_error = message
            raise RuntimeError(message)


_backup_restore_maintenance = _BackupRestoreMaintenance()


def _prepare_backup_restore_maintenance() -> None:
    """Stop writers before a restore replaces the database and local files."""
    _backup_restore_maintenance.stop()
    logger.warning({
        "event": "backup_restore_maintenance_started",
        "requires_restart": True,
    })


def _finish_backup_restore_maintenance() -> None:
    # Restored in-memory repositories and workers must not continue using
    # pre-restore state.  The restore response explicitly requires a process
    # restart instead of silently trying to rebuild live services.
    logger.warning({
        "event": "backup_restore_maintenance_finished",
        "requires_restart": True,
    })


def _configure_threadpool() -> None:
    raw_tokens = env_value(
        "GPTIMAGE2API_THREAD_TOKENS",
        "CHATGPT2API_THREAD_TOKENS",
        default=str(DEFAULT_THREAD_TOKENS),
    )
    try:
        tokens = max(1, int(raw_tokens))
    except (TypeError, ValueError):
        tokens = DEFAULT_THREAD_TOKENS
    limiter = current_default_thread_limiter()
    previous = int(getattr(limiter, "total_tokens", 0) or 0)
    if previous != tokens:
        limiter.total_tokens = tokens
    realtime_monitor_service.set_threadpool(tokens=tokens, previous_tokens=previous)
    logger.info({
        "event": "runtime_threadpool_configured",
        "previous_tokens": previous,
        "tokens": tokens,
    })


def create_app() -> FastAPI:
    config.require_bootstrap_auth_key()
    app_version = config.app_version
    backup_service.configure_restore_hooks(
        _prepare_backup_restore_maintenance,
        _finish_backup_restore_maintenance,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        genbox_started = False
        image_queue_started = False
        backup_started = False
        thread = None
        cleanup_thread = None
        dashboard_metrics_thread = None
        register_scheduler_thread = None
        try:
            _configure_threadpool()
            # Mark each service before start().  Thread creation can fail after
            # partially allocating resources; shutdown methods are idempotent
            # and must still get a chance to release those resources.
            genbox_started = True
            start_genbox_push_service()
            # Mark the queue for cleanup before calling start().  Startup can
            # allocate resources and then fail before ImageTaskService gets a
            # chance to report itself as fully started.
            image_queue_started = True
            image_task_service.start()
            try:
                projection_reset = await run_in_threadpool(
                    dashboard_metrics_service.reset_projection_schema_if_needed
                )
                if projection_reset.changed:
                    logger.info({
                        "event": "dashboard_metrics_projection_schema_reset",
                        "state_recreated": projection_reset.state_recreated,
                        "hourly_recreated": projection_reset.hourly_recreated,
                        "model_hourly_recreated": projection_reset.model_hourly_recreated,
                    })
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_projection_schema_reset_failed", "error": str(exc)})
            try:
                cleanup_result = await run_in_threadpool(
                    retention_cleanup_coordinator.run_startup_automatic,
                    enforce_image_free_space=False,
                )
                cleanup_errors = cleanup_result.get("errors") or {}
                if cleanup_errors.get("logs"):
                    logger.error({"event": "log_startup_cleanup_failed", "error": cleanup_errors["logs"]})
                if cleanup_errors.get("images"):
                    logger.error({"event": "image_startup_cleanup_failed", "error": cleanup_errors["images"]})
            except Exception as exc:
                logger.error({"event": "retention_startup_cleanup_failed", "error": str(exc)})
            try:
                await run_in_threadpool(
                    dashboard_metrics_service.sync_from_log_service,
                    log_service,
                )
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_startup_sync_failed", "error": str(exc)})
            account_service.cleanup_auto_remove_accounts()
            thread = start_account_lifecycle_watcher(stop_event)
            cleanup_thread = start_retention_cleanup_scheduler(stop_event)
            dashboard_metrics_thread = dashboard_metrics_service.start_refresh_scheduler(
                log_service,
                stop_event,
            )
            register_scheduler_thread = register_service.start_auto_scheduler(stop_event)
            backup_started = True
            backup_service.start()
            _backup_restore_maintenance.activate(
                stop_event=stop_event,
                threads=(
                    thread,
                    cleanup_thread,
                    dashboard_metrics_thread,
                    register_scheduler_thread,
                ),
                service_stops=(
                    ("register", lambda: register_service.shutdown(30)),
                    ("image_queue", lambda: image_task_service.stop(30)),
                    ("genbox", shutdown_genbox_push_service),
                    ("backup", backup_service.stop),
                ),
            )
            yield
        finally:
            stop_event.set()
            if thread is not None:
                thread.join(timeout=1)
            if dashboard_metrics_thread is not None:
                dashboard_metrics_thread.join(timeout=1)
            if register_scheduler_thread is not None:
                register_scheduler_thread.join(timeout=1)
            if cleanup_thread is not None:
                await run_in_threadpool(cleanup_thread.join, RETENTION_SHUTDOWN_TIMEOUT_SECS)
            try:
                await run_in_threadpool(register_service.shutdown, 30)
            except Exception as exc:
                logger.error({
                    "event": "register_shutdown_failed",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                })
            if image_queue_started:
                try:
                    await run_in_threadpool(image_task_service.stop, 30)
                except Exception as exc:
                    logger.error({
                        "event": "image_queue_shutdown_failed",
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    })
            if genbox_started:
                try:
                    await run_in_threadpool(shutdown_genbox_push_service)
                except Exception as exc:
                    logger.error({
                        "event": "genbox_shutdown_failed",
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    })
            try:
                await run_in_threadpool(
                    dashboard_metrics_service.sync_from_log_service,
                    log_service,
                )
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_shutdown_sync_failed", "error": str(exc)})
            if backup_started:
                try:
                    backup_service.stop()
                except Exception as exc:
                    logger.error({
                        "event": "backup_shutdown_failed",
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    })
            _backup_restore_maintenance.deactivate()
    app = FastAPI(title="gptimage2api", version=app_version, lifespan=lifespan)

    @app.middleware("http")
    async def add_generated_request_identity_header(request, call_next):
        response = await call_next(request)
        if getattr(request.state, "gptimage2api_idempotency_key_generated", False):
            key = str(
                getattr(request.state, "gptimage2api_idempotency_key", "") or ""
            ).strip()
            if key:
                response.headers["Idempotency-Key"] = key
        return response

    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Export-Requested",
            "X-Exported",
            "X-Skipped",
            "Idempotency-Key",
        ],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(register.create_router())
    app.include_router(prompts.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(asset)

    return app
