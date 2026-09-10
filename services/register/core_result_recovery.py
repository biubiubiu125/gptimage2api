from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

from services.account_service import account_service as default_account_service
from services.register import openai_register
from services.register.log_redaction import redact_register_log_text


MAX_RECOVERY_BATCH = 20
MAX_RETRY_DELAY_SECONDS = 60 * 60
MAX_RECOVERY_RETRY_COUNT = 5
TERMINAL_RECOVERY_STATUSES = {"failed_terminal"}
_reconcile_lock = RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _secret_values(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get(key) or "").strip()
        for key in ("access_token", "refresh_token", "id_token", "password")
        if str(row.get(key) or "").strip()
    ]


def _safe_error(error: object, row: dict[str, Any]) -> str:
    value = redact_register_log_text(error)
    for secret in _secret_values(row):
        value = value.replace(secret, "***")
    return value[:500]


def _read_rows() -> list[dict[str, Any]]:
    with openai_register.register_core_results_lock:
        return deepcopy(openai_register._pending_core_result_rows())


def _update_row(access_token: str, updates: dict[str, Any]) -> bool:
    with openai_register.register_core_results_lock:
        rows = openai_register._pending_core_result_rows()
        changed = False
        for row in rows:
            if str(row.get("access_token") or "").strip() != access_token:
                continue
            row.update(updates)
            changed = True
            break
        if changed:
            openai_register.write_pending_core_result_rows(rows)
        return changed


def _projection(row: dict[str, Any]) -> dict[str, Any]:
    projection = {
        key: row.get(key)
        for key in (
            "email",
            "source_type",
            "created_at",
            "register_stage",
            "task_index",
            "updated_at",
            "status",
            "retry_count",
            "last_error",
            "next_retry_at",
            "terminal_at",
            "terminal_reason",
        )
        if row.get(key) not in (None, "")
    }
    if "last_error" in projection:
        projection["last_error"] = _safe_error(projection["last_error"], row)
    return projection


def list_pending_core_results() -> list[dict[str, Any]]:
    """Return an admin-safe view without access, refresh, id, or mailbox tokens."""
    return [_projection(row) for row in _read_rows()]


def _reconcile_core_result_unlocked(
    result: dict[str, Any],
    *,
    register_proxy: str = "",
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    account_service_obj: Any = default_account_service,
) -> dict[str, Any]:
    access_token = str(result.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("注册核心结果缺少 access_token")

    if verify_fn is None:
        verify_fn = openai_register.verify_registered_account
    remote_info = verify_fn(access_token, register_proxy=register_proxy)
    if not isinstance(remote_info, dict):
        raise RuntimeError("注册账号验活结果格式无效")

    normalized = {
        **result,
        **{
            key: value
            for key, value in remote_info.items()
            if value not in (None, "")
        },
    }
    persist_result = account_service_obj.add_account_items(
        [normalized],
        return_items=False,
    )
    if isinstance(persist_result, dict) and persist_result.get("errors"):
        raise RuntimeError(f"账号入库失败: {persist_result['errors']}")

    refresh_result = account_service_obj.refresh_accounts([access_token])
    if not isinstance(refresh_result, dict):
        raise RuntimeError("账号池刷新结果格式无效")
    if refresh_result.get("errors"):
        raise RuntimeError(f"账号池刷新失败: {refresh_result['errors']}")

    remove_error = openai_register.remove_pending_core_result(access_token)
    if remove_error:
        raise RuntimeError(f"注册核心暂存结果清理失败: {remove_error}")
    return {
        "result": normalized,
        "warnings": [],
    }


def reconcile_core_result(
    result: dict[str, Any],
    *,
    register_proxy: str = "",
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    account_service_obj: Any = default_account_service,
) -> dict[str, Any]:
    """Serialize direct and scheduled handoff of a registration core result."""
    with _reconcile_lock:
        return _reconcile_core_result_unlocked(
            result,
            register_proxy=register_proxy,
            verify_fn=verify_fn,
            account_service_obj=account_service_obj,
        )


def reconcile_pending_core_results(
    *,
    register_proxy: str = "",
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    account_service_obj: Any = default_account_service,
    limit: int = MAX_RECOVERY_BATCH,
    force: bool = False,
    access_token: str = "",
    email: str = "",
) -> dict[str, Any]:
    try:
        batch_limit = max(1, min(MAX_RECOVERY_BATCH, int(limit or MAX_RECOVERY_BATCH)))
    except (TypeError, ValueError):
        batch_limit = MAX_RECOVERY_BATCH
    with _reconcile_lock:
        rows = _read_rows()
        now = _now()
        selected = 0
        succeeded = 0
        failed = 0
        skipped = 0
        errors: list[str] = []
        target_token = str(access_token or "").strip()
        target_email = str(email or "").strip().casefold()

        for row in rows:
            token = str(row.get("access_token") or "").strip()
            row_email = str(row.get("email") or "").strip().casefold()
            if (
                not token
                or (target_token and token != target_token)
                or (target_email and row_email != target_email)
            ):
                continue
            if str(row.get("status") or "").strip() in TERMINAL_RECOVERY_STATUSES and not force:
                skipped += 1
                continue
            next_retry_at = _parse_time(row.get("next_retry_at"))
            if not force and next_retry_at is not None and next_retry_at > now:
                skipped += 1
                continue
            if selected >= batch_limit:
                break
            selected += 1
            try:
                outcome = reconcile_core_result(
                    row,
                    register_proxy=register_proxy,
                    verify_fn=verify_fn,
                    account_service_obj=account_service_obj,
                )
                succeeded += 1
                warnings = outcome.get("warnings") or []
                errors.extend(str(item) for item in warnings)
            except Exception as exc:
                failed += 1
                try:
                    retry_count = max(0, int(row.get("retry_count") or 0)) + 1
                except (TypeError, ValueError):
                    retry_count = 1
                delay = min(MAX_RETRY_DELAY_SECONDS, 30 * (2 ** min(retry_count - 1, 7)))
                error_text = _safe_error(exc, row)
                terminal = retry_count >= MAX_RECOVERY_RETRY_COUNT
                try:
                    _update_row(
                        token,
                        {
                            "status": "failed_terminal" if terminal else "retrying",
                            "retry_count": retry_count,
                            "last_error": error_text,
                            "failure_reason": error_text,
                            "next_retry_at": "" if terminal else (now + timedelta(seconds=delay)).isoformat(),
                            "terminal_at": _now_text() if terminal else str(row.get("terminal_at") or ""),
                            "terminal_reason": error_text if terminal else str(row.get("terminal_reason") or ""),
                            "updated_at": _now_text(),
                        },
                    )
                except Exception as update_error:
                    error_text = (
                        f"{error_text}; recovery state update failed: "
                        f"{_safe_error(update_error, row)}"
                    )[:500]
                errors.append(f"{row.get('email') or 'pending-result'}: {error_text}")

        return {
            "attempted": selected,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "remaining": len(_read_rows()),
        }
