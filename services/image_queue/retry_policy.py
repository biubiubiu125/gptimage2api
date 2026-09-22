from __future__ import annotations

from datetime import datetime, timedelta
import random

from services.image_failure import (
    classify_image_exception,
    should_switch_generating_account,
    should_switch_image_account,
)
from services.image_queue.sanitization import original_queue_error_message
from services.image_queue.settings import ImageQueueSettings
from services.image_queue.types import JobStage, RetryDecision


def _image_account_retry_settings() -> tuple[bool, int]:
    from services.config import config

    enabled = bool(config.image_account_retry_enabled)
    try:
        max_attempts = max(2, int(config.image_max_account_attempts or 2))
    except (TypeError, ValueError):
        max_attempts = 2
    return enabled, max_attempts


class RetryPolicy:
    def __init__(self, settings: ImageQueueSettings, random_source: random.Random | None = None) -> None:
        self.settings = settings
        self.random = random_source or random.Random()

    def _download_or_save_stage(self, stage: JobStage) -> bool:
        return stage in {
            JobStage.RESOLVING,
            JobStage.DOWNLOADING,
            JobStage.TRANSFORMING,
            JobStage.SAVING,
        }

    def _budget(self, stage: JobStage, *, account_retry_enabled: bool, max_account_attempts: int) -> int:
        if stage in {JobStage.RESOLVING, JobStage.DOWNLOADING}:
            return self.settings.download_attempts
        if stage in {JobStage.TRANSFORMING, JobStage.SAVING}:
            return self.settings.save_attempts
        if account_retry_enabled:
            return max(2, int(max_account_attempts or 2))
        return self.settings.generation_attempts

    def decision(
        self,
        stage: JobStage | str,
        attempts: int,
        error: BaseException,
        now: datetime,
    ) -> RetryDecision:
        resolved_stage = stage if isinstance(stage, JobStage) else JobStage(str(stage))
        failure = classify_image_exception(error)
        used = max(1, int(attempts or 1))
        generating = not self._download_or_save_stage(resolved_stage)
        account_retry_enabled, max_account_attempts = (
            _image_account_retry_settings() if generating else (False, 0)
        )
        generating_switch = (
            generating
            and account_retry_enabled
            and should_switch_generating_account(failure.code)
        )
        hard_fail = failure.code in {
            "content_policy_violation",
            "invalid_image_input",
            "upstream_text_reply",
            "unsupported_model",
            "task_interrupted",
            "image_task_cancelled",
            "internal_error",
            "durable_context_required",
        }
        if hard_fail:
            transient = False
        elif generating_switch:
            transient = True
        elif generating:
            transient = bool(failure.retryable)
        else:
            transient = failure.retryable or should_switch_image_account(failure.code)
        error_message = original_queue_error_message(error, failure)
        budget = self._budget(
            resolved_stage,
            account_retry_enabled=account_retry_enabled,
            max_account_attempts=max_account_attempts,
        )
        if not transient or used >= budget:
            return RetryDecision(False, error_code=failure.code, error_message=error_message)
        if generating_switch or (generating and int(failure.status_code) == 400):
            delay = 0.0
        else:
            delay = min(300.0, 5.0 * (3 ** (used - 1))) + self.random.uniform(0.0, 1.0)
        return RetryDecision(
            True,
            next_retry_at=now + timedelta(seconds=delay),
            error_code=failure.code,
            error_message=error_message,
        )
