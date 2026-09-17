from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import socket

import psutil

from services.config import config
from services.database_url import IMAGE_QUEUE_DATABASE_NAME, build_postgres_url_from_env, select_named_postgres_database


DEFAULT_PENDING_TTL_SECONDS = 30 * 60
DEFAULT_GENERATION_CONCURRENCY_HARD_CAP = 99999
MAX_GENERATION_CONCURRENCY_HARD_CAP = 99999
MAX_ABSOLUTE_GUARD = 99999
AUTO_GENERATION_CONCURRENCY_FLOOR = 300
AUTO_GENERATION_CONCURRENCY_PER_CORE = 300
FALLBACK_AVAILABLE_MEMORY_BYTES = 8 * 1024**3
DEFAULT_PROMPT_SUFFIX = (
    "请直接生成最终图片，只输出图片结果，不要回复解释、拒绝说明、文字描述或 Markdown。"
    "高清画质，细节丰富，主体清晰，构图完整。"
)


class ImageQueueConfigurationError(RuntimeError):
    pass


def _first_env_value(name: str | tuple[str, ...], default: object) -> object:
    raw_names = (name,) if isinstance(name, str) else name
    names: list[str] = []
    for candidate in raw_names:
        candidate = str(candidate)
        if candidate not in names:
            names.append(candidate)
        if candidate.startswith(("IMAGE_QUEUE_", "IMAGE_PROMPT_")):
            for prefix in ("GPTIMAGE2API_", "CHATGPT2API_"):
                alias = f"{prefix}{candidate}"
                if alias not in names:
                    names.append(alias)
    for candidate in names:
        raw = os.getenv(candidate)
        if raw is not None and str(raw).strip():
            return raw
    return default


def _coerce_int(value: object, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError):
        normalized = default
    normalized = max(minimum, normalized)
    return min(normalized, maximum) if maximum is not None else normalized


def _env_int(name: str | tuple[str, ...], default: int, minimum: int = 1, maximum: int | None = None) -> int:
    return _coerce_int(_first_env_value(name, default), default, minimum, maximum)


def _env_float(name: str | tuple[str, ...], default: float, minimum: float = 0.0) -> float:
    try:
        value = float(str(_first_env_value(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_bool(name: str | tuple[str, ...], default: bool) -> bool:
    raw = str(_first_env_value(name, "")).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "disabled", "none", "null", ""}:
        return False
    return default


def _read_cgroup_number(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if not value or value == "max":
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _read_first_cgroup_number(*relative_paths: str, cgroup_root: Path = Path("/sys/fs/cgroup")) -> int | None:
    for relative_path in relative_paths:
        value = _read_cgroup_number(cgroup_root / relative_path)
        if value is not None:
            return value
    return None


def _cgroup_cpu_limit_cores(cgroup_root: Path = Path("/sys/fs/cgroup")) -> float | None:
    try:
        raw = (cgroup_root / "cpu.max").read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeError):
        raw = None
    else:
        # v2 cpu.max 文件存在时以它为准。"max" 表示不限额，不再读残留 v1 quota。
        try:
            if len(raw) == 2 and raw[0] != "max":
                quota = int(raw[0])
                period = int(raw[1])
                if quota > 0 and period > 0:
                    return float(quota) / float(period)
        except (ValueError, ZeroDivisionError):
            return None
        return None
    quota = _read_first_cgroup_number(
        "cpu.cfs_quota_us",
        "cpu/cpu.cfs_quota_us",
        "cpuacct/cpu.cfs_quota_us",
        "cpu,cpuacct/cpu.cfs_quota_us",
        cgroup_root=cgroup_root,
    )
    period = _read_first_cgroup_number(
        "cpu.cfs_period_us",
        "cpu/cpu.cfs_period_us",
        "cpuacct/cpu.cfs_period_us",
        "cpu,cpuacct/cpu.cfs_period_us",
        cgroup_root=cgroup_root,
    )
    if quota is None or period is None or quota <= 0 or period <= 0:
        return None
    return float(quota) / float(period)


def _detected_cpu_cores() -> int:
    cgroup_limit = _cgroup_cpu_limit_cores()
    if cgroup_limit is not None:
        return max(1, int(math.ceil(cgroup_limit)))
    try:
        return max(1, int(os.cpu_count() or 1))
    except (TypeError, ValueError):
        return 1


def _detected_available_memory_bytes() -> int:
    try:
        memory = psutil.virtual_memory()
        physical_limit = int(memory.total)
        physical_available = int(memory.available)
    except Exception:
        return FALLBACK_AVAILABLE_MEMORY_BYTES
    cgroup_limit = _read_first_cgroup_number(
        "memory.max",
        "memory.limit_in_bytes",
        "memory/memory.limit_in_bytes",
    )
    cgroup_current = _read_first_cgroup_number(
        "memory.current",
        "memory.usage_in_bytes",
        "memory/memory.usage_in_bytes",
    )
    if (
        cgroup_limit is None
        or cgroup_current is None
        or cgroup_limit <= 0
        or cgroup_limit >= physical_limit
    ):
        return max(1, physical_available)
    return max(1, min(physical_available, max(0, cgroup_limit - cgroup_current)))


def _estimated_worker_floor_threads(cpu_cores: int) -> int:
    cpu_cores = max(1, int(cpu_cores))
    recovery = max(4, min(32, cpu_cores * 2))
    io_threads = max(4, min(32, cpu_cores * 2))
    upscale = max(1, min(16, cpu_cores))
    return recovery + io_threads + upscale


def _adaptive_generation_concurrency_default(
    *,
    runtime_limit: int | None = None,
    cpu_cores: int | None = None,
    available_memory_bytes: int | None = None,
) -> int:
    del available_memory_bytes
    cpu_cores = max(1, int(cpu_cores or _detected_cpu_cores()))
    auto_limit = max(
        AUTO_GENERATION_CONCURRENCY_FLOOR,
        cpu_cores * AUTO_GENERATION_CONCURRENCY_PER_CORE,
    )
    if runtime_limit is None:
        return max(1, auto_limit)
    return max(1, min(int(runtime_limit), auto_limit))


def resolve_generation_concurrency_limit(*, cpu_cores: int | None = None) -> int:
    hard_cap = _coerce_int(
        _first_env_value("IMAGE_QUEUE_GENERATION_CONCURRENCY_CAP", 0),
        0,
        0,
        MAX_GENERATION_CONCURRENCY_HARD_CAP,
    )
    if hard_cap <= 0:
        hard_cap = DEFAULT_GENERATION_CONCURRENCY_HARD_CAP
    generation_limit = _coerce_int(
        _first_env_value("IMAGE_QUEUE_GENERATION_CONCURRENCY", 0),
        0,
        0,
        hard_cap,
    )
    if generation_limit <= 0:
        generation_limit = _adaptive_generation_concurrency_default(cpu_cores=cpu_cores)
    return max(1, min(generation_limit, hard_cap))


AUTO_DATABASE_POOL_SIZE_CAP = 80
AUTO_DATABASE_MAX_OVERFLOW_CAP = 40


def _auto_database_pool_size(generation_limit: int) -> int:
    return max(20, min(AUTO_DATABASE_POOL_SIZE_CAP, max(1, int(generation_limit)) // 8))


def _auto_database_max_overflow(generation_limit: int) -> int:
    return max(10, min(AUTO_DATABASE_MAX_OVERFLOW_CAP, max(1, int(generation_limit)) // 16))


def _adaptive_absolute_guard_default(
    generation_concurrency: int,
    *,
    cpu_cores: int | None = None,
) -> int:
    cpu_cores = max(1, int(cpu_cores or _detected_cpu_cores()))
    floor_threads = _estimated_worker_floor_threads(cpu_cores)
    generation_threads = max(8, int(generation_concurrency or 1))
    door_tokens = generation_threads
    process_reserve = max(64, cpu_cores * 8)
    return max(
        8,
        floor_threads + generation_threads + door_tokens + process_reserve,
    )


def _runtime_image_concurrency_default() -> int:
    return _adaptive_generation_concurrency_default()


@dataclass(frozen=True)
class ImageQueueSettings:
    database_url: str
    lease_seconds: int = 90
    heartbeat_seconds: int = 15
    claim_max_runtime_seconds: int = 30 * 60
    poll_interval_seconds: float = 0.5
    result_wait_poll_seconds: float = 0.25
    protocol_wait_timeout_seconds: int = 300
    generation_attempts: int = 3
    download_attempts: int = 5
    save_attempts: int = 5
    recovery_account_timeout_seconds: int = 900
    delivery_grace_seconds: int = 7 * 24 * 60 * 60
    terminal_retention_seconds: int = 30 * 24 * 60 * 60
    occupancy_pause_percent: float = 90.0
    occupancy_resume_percent: float = 80.0
    occupancy_hold_seconds: float = 2.5
    # 0 means "auto": compute a machine-aware default in __post_init__.
    absolute_guard: int = 0
    generation_concurrency_limit: int = 0
    generation_concurrency_hard_cap: int = 0
    max_backlog: int = 50
    orphan_artifact_retention_seconds: int = 60 * 60
    pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS
    prompt_suffix_enabled: bool = True
    prompt_suffix: str = DEFAULT_PROMPT_SUFFIX
    database_pool_size: int = 20
    database_max_overflow: int = 10
    artifact_root: Path = Path("data/images")
    legacy_task_path: Path = Path("data/image_tasks.json")
    instance_id: str = ""
    verify_returned_url: bool = True
    returned_url_verify_timeout_seconds: float = 5.0
    returned_url_verify_attempts: int = 3
    returned_url_verify_max_bytes: int = 65536

    @property
    def available(self) -> bool:
        return bool(self.database_url)

    def __post_init__(self) -> None:
        hard_cap = _coerce_int(
            self.generation_concurrency_hard_cap,
            0,
            0,
            MAX_GENERATION_CONCURRENCY_HARD_CAP,
        )
        if hard_cap <= 0:
            hard_cap = DEFAULT_GENERATION_CONCURRENCY_HARD_CAP

        generation_limit = _coerce_int(self.generation_concurrency_limit, 0, 0, hard_cap)
        if generation_limit <= 0:
            generation_limit = _runtime_image_concurrency_default()
        generation_limit = _coerce_int(generation_limit, 1, 1, hard_cap)

        absolute_guard = _coerce_int(self.absolute_guard, 0, 0, MAX_ABSOLUTE_GUARD)
        if absolute_guard <= 0:
            absolute_guard = _adaptive_absolute_guard_default(generation_limit)
        absolute_guard = _coerce_int(absolute_guard, 8, 8, MAX_ABSOLUTE_GUARD)

        occupancy_pause = max(1.0, float(self.occupancy_pause_percent or 90.0))
        occupancy_resume = max(1.0, float(self.occupancy_resume_percent or 80.0))
        if occupancy_resume >= occupancy_pause:
            occupancy_resume = max(1.0, occupancy_pause - 10.0)
        occupancy_hold = max(0.1, float(self.occupancy_hold_seconds or 2.5))

        pool_size = _coerce_int(self.database_pool_size, 0, 0, 400)
        if pool_size <= 0:
            pool_size = _auto_database_pool_size(generation_limit)
        overflow = int(self.database_max_overflow)
        try:
            overflow = int(overflow)
        except (TypeError, ValueError):
            overflow = -1
        if overflow < 0:
            overflow = _auto_database_max_overflow(generation_limit)
        overflow = max(0, min(200, overflow))

        object.__setattr__(self, "generation_concurrency_hard_cap", hard_cap)
        object.__setattr__(self, "generation_concurrency_limit", generation_limit)
        object.__setattr__(self, "absolute_guard", absolute_guard)
        object.__setattr__(self, "occupancy_pause_percent", occupancy_pause)
        object.__setattr__(self, "occupancy_resume_percent", occupancy_resume)
        object.__setattr__(self, "occupancy_hold_seconds", occupancy_hold)
        object.__setattr__(self, "database_pool_size", pool_size)
        object.__setattr__(self, "database_max_overflow", overflow)

    @classmethod
    def from_env(cls) -> "ImageQueueSettings":
        try:
            database_url = select_named_postgres_database(
                dedicated_url=(
                    _first_env_value(
                        (
                            "GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL",
                            "CHATGPT2API_IMAGE_QUEUE_DATABASE_URL",
                        ),
                        "",
                    )
                    or os.getenv("IMAGE_QUEUE_DATABASE_URL")
                ),
                fallback_url=(
                    _first_env_value(
                        (
                            "GPTIMAGE2API_DATABASE_URL",
                            "CHATGPT2API_DATABASE_URL",
                        ),
                        "",
                    )
                    or os.getenv("DATABASE_URL")
                ),
                expected_name=IMAGE_QUEUE_DATABASE_NAME,
                role="image queue",
            )
            if not database_url:
                local_database_url = build_postgres_url_from_env(IMAGE_QUEUE_DATABASE_NAME)
                if local_database_url:
                    database_url = select_named_postgres_database(
                        dedicated_url=local_database_url,
                        fallback_url="",
                        expected_name=IMAGE_QUEUE_DATABASE_NAME,
                        role="image queue",
                    )
        except ValueError as exc:
            raise ImageQueueConfigurationError(str(exc)) from exc
        root = Path(str(_first_env_value("IMAGE_QUEUE_ARTIFACT_ROOT", config.images_dir)).strip())
        legacy_task_path = Path(
            str(_first_env_value("IMAGE_QUEUE_LEGACY_TASK_PATH", "data/image_tasks.json")).strip()
        )
        instance_id = str(
            _first_env_value(
                (
                    "GPTIMAGE2API_IMAGE_QUEUE_INSTANCE_ID",
                    "CHATGPT2API_IMAGE_QUEUE_INSTANCE_ID",
                    "IMAGE_QUEUE_INSTANCE_ID",
                ),
                os.getenv("HOSTNAME") or socket.gethostname() or "gptimage2api",
            )
        ).strip()[:120]
        return cls(
            database_url=database_url,
            lease_seconds=_env_int("IMAGE_QUEUE_LEASE_SECONDS", 90, 30, 900),
            heartbeat_seconds=_env_int("IMAGE_QUEUE_HEARTBEAT_SECONDS", 15, 5, 300),
            claim_max_runtime_seconds=_env_int(
                "IMAGE_QUEUE_CLAIM_MAX_RUNTIME_SECONDS", 30 * 60, 60, 24 * 60 * 60
            ),
            poll_interval_seconds=_env_float("IMAGE_QUEUE_POLL_INTERVAL_SECONDS", 0.5, 0.05),
            result_wait_poll_seconds=_env_float("IMAGE_QUEUE_RESULT_WAIT_POLL_SECONDS", 0.25, 0.05),
            protocol_wait_timeout_seconds=_env_int(
                "IMAGE_QUEUE_PROTOCOL_WAIT_TIMEOUT_SECONDS", 300, 30, 3600
            ),
            generation_attempts=_env_int("IMAGE_QUEUE_GENERATION_ATTEMPTS", 3, 1, 10),
            download_attempts=_env_int("IMAGE_QUEUE_DOWNLOAD_ATTEMPTS", 5, 1, 20),
            save_attempts=_env_int("IMAGE_QUEUE_SAVE_ATTEMPTS", 5, 1, 20),
            recovery_account_timeout_seconds=_env_int(
                "IMAGE_QUEUE_RECOVERY_ACCOUNT_TIMEOUT_SECONDS", 900, 30, 86400
            ),
            delivery_grace_seconds=_env_int(
                "IMAGE_QUEUE_DELIVERY_GRACE_SECONDS", 7 * 24 * 60 * 60, 3600, 30 * 24 * 60 * 60
            ),
            terminal_retention_seconds=_env_int(
                "IMAGE_QUEUE_TERMINAL_RETENTION_SECONDS",
                30 * 24 * 60 * 60,
                24 * 60 * 60,
                365 * 24 * 60 * 60,
            ),
            occupancy_pause_percent=_env_float("IMAGE_QUEUE_OCCUPANCY_PAUSE_PERCENT", 90.0, 1.0),
            occupancy_resume_percent=_env_float("IMAGE_QUEUE_OCCUPANCY_RESUME_PERCENT", 80.0, 1.0),
            occupancy_hold_seconds=_env_float("IMAGE_QUEUE_OCCUPANCY_HOLD_SECONDS", 2.5, 0.1),
            absolute_guard=_env_int("IMAGE_QUEUE_ABSOLUTE_GUARD", 0, 0, MAX_ABSOLUTE_GUARD),
            generation_concurrency_limit=_env_int(
                "IMAGE_QUEUE_GENERATION_CONCURRENCY",
                0,
                0,
                MAX_GENERATION_CONCURRENCY_HARD_CAP,
            ),
            generation_concurrency_hard_cap=_env_int(
                "IMAGE_QUEUE_GENERATION_CONCURRENCY_CAP",
                0,
                0,
                MAX_GENERATION_CONCURRENCY_HARD_CAP,
            ),
            max_backlog=_env_int("IMAGE_QUEUE_MAX_BACKLOG", 50, 1, 100000),
            orphan_artifact_retention_seconds=_env_int(
                "IMAGE_QUEUE_ORPHAN_ARTIFACT_RETENTION_SECONDS",
                60 * 60,
                60,
                30 * 24 * 60 * 60,
            ),
            pending_ttl_seconds=_env_int(
                "IMAGE_QUEUE_PENDING_TTL_SECONDS",
                DEFAULT_PENDING_TTL_SECONDS,
                1,
                365 * 24 * 60 * 60,
            ),
            prompt_suffix_enabled=_env_bool("IMAGE_PROMPT_SUFFIX_ENABLED", True),
            prompt_suffix=str(_first_env_value("IMAGE_PROMPT_SUFFIX", DEFAULT_PROMPT_SUFFIX)).strip(),
            database_pool_size=_env_int("IMAGE_QUEUE_DB_POOL_SIZE", 0, 0, 400),
            database_max_overflow=_env_int("IMAGE_QUEUE_DB_MAX_OVERFLOW", -1, -1, 200),
            artifact_root=root,
            legacy_task_path=legacy_task_path,
            instance_id=instance_id,
            verify_returned_url=_env_bool(
                "IMAGE_QUEUE_VERIFY_RETURNED_URL",
                True,
            ),
            returned_url_verify_timeout_seconds=_env_float(
                "IMAGE_QUEUE_RETURNED_URL_VERIFY_TIMEOUT_SECONDS",
                5.0,
                0.5,
            ),
            returned_url_verify_attempts=_env_int(
                "IMAGE_QUEUE_RETURNED_URL_VERIFY_ATTEMPTS",
                3,
                1,
                10,
            ),
            returned_url_verify_max_bytes=_env_int(
                "IMAGE_QUEUE_RETURNED_URL_VERIFY_MAX_BYTES",
                65536,
                512,
                8 * 1024 * 1024,
            ),
        )
