from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import time
from threading import RLock
from typing import Any, Callable

import psutil

from services.image_failure import image_failure
from services.image_queue.settings import (
    FALLBACK_AVAILABLE_MEMORY_BYTES,
    ImageQueueConfigurationError,
    ImageQueueSettings,
)
from services.image_queue.types import ResourceDecision, ResourceSnapshot
from utils.log import logger


class ImageQueueStorageFullError(RuntimeError):
    code = "image_queue_storage_full"

    def __init__(self, message: str = "image artifact storage is under disk pressure") -> None:
        super().__init__(message)
        self.failure = image_failure(self.code, raw_detail=message).with_public_detail(message)


class ImageQueueResourcePressureError(RuntimeError):
    code = "image_queue_resource_pressure"

    def __init__(self, reason: str = "resource_pressure") -> None:
        message = f"image queue is temporarily unavailable due to {reason}"
        super().__init__(message)
        self.reason = reason
        self.failure = image_failure(self.code, raw_detail=message).with_public_detail(message)


class ResourceController:
    FILE_HANDLE_GUARD = 8192
    UPSTREAM_ERROR_WINDOW = 40
    UPSTREAM_ERROR_RATE_THRESHOLD = 0.45
    UPSTREAM_ERROR_MIN_SAMPLES = 8
    UPSTREAM_ERROR_SAMPLE_TTL_SECONDS = 60.0
    CPU_OCCUPANCY_MIN_INTERVAL = 0.2
    GENERATION_GATE_REASONS = frozenset({
        "resource_cpu",
        "resource_memory",
        "resource_swap",
        "resource_occupancy",
        "resource_disk",
        "resource_threads",
        "resource_file_handles",
        "resource_database_pool",
        "resource_upstream_errors",
        "resource_pressure",
        "resource_paused",
    })

    def __init__(
        self,
        settings: ImageQueueSettings,
        *,
        database: Any | None = None,
        cgroup_root: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.database = database
        self.cgroup_root = cgroup_root or Path("/sys/fs/cgroup")
        self._monotonic = monotonic
        self._last_swap_sample: tuple[int, int, float] | None = None
        self._last_cpu_sample: tuple[int, float] | None = None
        self._occupancy_paused = False
        self._occupancy_pause_reason = ""
        self._occupancy_high_since: float | None = None
        self._occupancy_low_since: float | None = None
        self._occupancy_cpu_sample: tuple[float, float] | None = None
        self._lock = RLock()
        self._probe_lock = RLock()
        self._probe_cache: tuple[float, ResourceSnapshot] | None = None
        self._last_cpu_percent: float | None = None
        self._host_cpu_times_sample: tuple[float, float, float, float] | None = None
        self._upstream_outcomes: deque[tuple[float, bool]] = deque(maxlen=self.UPSTREAM_ERROR_WINDOW)
        self._prime_cpu_probe()

    @classmethod
    def generation_gate_closed(cls, occupancy_paused: bool, pause_reason: str = "") -> bool:
        if occupancy_paused:
            return True
        return str(pause_reason or "") in cls.GENERATION_GATE_REASONS

    @classmethod
    def from_runtime(
        cls,
        *,
        database: Any | None = None,
        settings: ImageQueueSettings | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> "ResourceController":
        if settings is None:
            try:
                settings = ImageQueueSettings.from_env()
            except ImageQueueConfigurationError:
                settings = ImageQueueSettings(database_url="")
        if monotonic is None:
            return cls(settings, database=database)
        return cls(settings, database=database, monotonic=monotonic)

    def _file_handle_guard(self) -> int:
        generation = max(1, int(getattr(self.settings, "generation_concurrency_limit", 1) or 1))
        return max(int(self.FILE_HANDLE_GUARD), generation * 8 + 1024)

    def _prime_cpu_probe(self) -> None:
        try:
            times = psutil.cpu_times()
            idle, total = self._cpu_times_idle_total(times)
            self._host_cpu_times_sample = (idle, total, float(self._monotonic()), 0.0)
        except Exception:
            pass
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        try:
            usage = self._cpu_usage_microseconds()
            if usage is not None:
                self._last_cpu_sample = (usage, self._monotonic())
        except Exception:
            pass

    @property
    def occupancy_paused(self) -> bool:
        with self._lock:
            return self._occupancy_paused

    @property
    def occupancy_pause_reason(self) -> str:
        with self._lock:
            return self._occupancy_pause_reason

    def fail_closed(self, reason: str = "resource_pressure") -> "ResourceController":
        with self._lock:
            self._occupancy_paused = True
            self._occupancy_pause_reason = str(reason or "resource_pressure")
            self._occupancy_high_since = float(self._monotonic())
            self._occupancy_low_since = None
        return self

    @staticmethod
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

    def _read_first_cgroup_number(self, *relative_paths: str) -> int | None:
        for relative_path in relative_paths:
            value = self._read_cgroup_number(self.cgroup_root / relative_path)
            if value is not None:
                return value
        return None

    def _memory_sample(self, memory: Any) -> tuple[int, int]:
        physical_limit = int(memory.total)
        physical_available = int(memory.available)
        cgroup_limit = self._read_first_cgroup_number(
            "memory.max",
            "memory.limit_in_bytes",
            "memory/memory.limit_in_bytes",
        )
        cgroup_current = self._read_first_cgroup_number(
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
            return physical_available, physical_limit
        return min(physical_available, max(0, cgroup_limit - cgroup_current)), cgroup_limit

    def cpu_limit_cores(self) -> float | None:
        try:
            raw = (self.cgroup_root / "cpu.max").read_text(encoding="ascii").strip().split()
            if len(raw) != 2 or raw[0] == "max":
                return None
            quota = int(raw[0])
            period = int(raw[1])
            return float(quota) / float(period) if quota > 0 and period > 0 else None
        except (OSError, UnicodeError, ValueError, ZeroDivisionError):
            pass
        quota = self._read_first_cgroup_number(
            "cpu.cfs_quota_us",
            "cpu/cpu.cfs_quota_us",
            "cpuacct/cpu.cfs_quota_us",
            "cpu,cpuacct/cpu.cfs_quota_us",
        )
        period = self._read_first_cgroup_number(
            "cpu.cfs_period_us",
            "cpu/cpu.cfs_period_us",
            "cpuacct/cpu.cfs_period_us",
            "cpu,cpuacct/cpu.cfs_period_us",
        )
        if quota is None or period is None or quota <= 0 or period <= 0:
            return None
        return float(quota) / float(period)

    def _cpu_usage_microseconds(self) -> int | None:
        try:
            values = {
                key: int(value)
                for key, value in (
                    line.split(None, 1)
                    for line in (self.cgroup_root / "cpu.stat").read_text(encoding="ascii").splitlines()
                    if len(line.split(None, 1)) == 2
                )
            }
            return max(0, int(values["usage_usec"]))
        except (OSError, UnicodeError, ValueError, KeyError):
            pass
        usage_ns = self._read_first_cgroup_number(
            "cpuacct.usage",
            "cpu/cpuacct.usage",
            "cpuacct/cpuacct.usage",
            "cpu,cpuacct/cpuacct.usage",
        )
        if usage_ns is not None:
            return int(usage_ns // 1000)
        usage_ticks = self._read_cpuacct_stat_ticks()
        if usage_ticks is None:
            return None
        try:
            clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, ValueError):
            clock_ticks = 100
        if clock_ticks <= 0:
            return None
        return int(usage_ticks * 1_000_000 / clock_ticks)

    def _read_cpuacct_stat_ticks(self) -> int | None:
        for relative_path in (
            "cpuacct.stat",
            "cpu/cpuacct.stat",
            "cpuacct/cpuacct.stat",
            "cpu,cpuacct/cpuacct.stat",
        ):
            try:
                lines = (self.cgroup_root / relative_path).read_text(encoding="ascii").splitlines()
            except (OSError, UnicodeError):
                continue
            values: dict[str, int] = {}
            for line in lines:
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                try:
                    values[parts[0]] = int(parts[1])
                except ValueError:
                    continue
            usage = values.get("user", 0) + values.get("system", 0)
            if usage > 0:
                return usage
        return None

    def _container_cpu_percent(self, fallback: float) -> float:
        limit_cores = self.cpu_limit_cores()
        if limit_cores is None:
            return float(fallback)
        usage_usec = self._cpu_usage_microseconds()
        if usage_usec is None:
            return float(fallback)
        now = self._monotonic()
        previous = self._last_cpu_sample
        if previous is None or now <= previous[1] or usage_usec < previous[0]:
            self._last_cpu_sample = (usage_usec, now)
            return float(fallback)
        elapsed = now - previous[1]
        if elapsed <= 0:
            if self._last_cpu_percent is not None:
                return float(self._last_cpu_percent)
            return float(fallback)
        self._last_cpu_sample = (usage_usec, now)
        used_seconds = float(usage_usec - previous[0]) / 1_000_000.0
        percent = max(0.0, min(100.0, used_seconds / elapsed / limit_cores * 100.0))
        self._last_cpu_percent = percent
        return percent

    @staticmethod
    def _file_handle_count(process: Any) -> int:
        last_error: BaseException | None = None
        tried = False
        for name in ("num_handles", "num_fds"):
            method = getattr(process, name, None)
            if not callable(method):
                continue
            tried = True
            try:
                return int(method())
            except (OSError, TypeError, ValueError) as exc:
                last_error = exc
                continue
        if tried:
            raise RuntimeError("file handle probe failed") from last_error
        raise RuntimeError("file handle probe unavailable")

    def _swap_rates(self, swap: Any) -> tuple[int, int]:
        now = self._monotonic()
        current = (int(getattr(swap, "sin", 0)), int(getattr(swap, "sout", 0)), now)
        previous = self._last_swap_sample
        self._last_swap_sample = current
        if previous is None or now <= previous[2]:
            return 0, 0
        elapsed = now - previous[2]
        return (
            int(max(0, current[0] - previous[0]) / elapsed),
            int(max(0, current[1] - previous[1]) / elapsed),
        )

    def _database_pool_percent(self) -> float:
        if self.database is None:
            if str(getattr(self.settings, "database_url", "") or "").strip():
                return 100.0
            return 0.0
        try:
            return float(self.database.pool_usage_percent())
        except Exception:
            return 100.0

    def note_upstream_outcome(
        self,
        *,
        success: bool,
        status_code: int | None = None,
        error_code: str = "",
    ) -> None:
        """Record a recent upstream generation/download outcome for adaptive pause."""
        with self._lock:
            now = float(self._monotonic())
            if success:
                self._upstream_outcomes.append((now, True))
                return
            code = str(error_code or "").strip().lower()
            try:
                numeric_status = int(status_code) if status_code is not None else None
            except (TypeError, ValueError):
                numeric_status = None
            transient = (
                numeric_status in {408, 429, 500, 502, 503, 504}
                or code in {
                    "upstream_timeout",
                    "upstream_5xx",
                    "upstream_rate_limited",
                    "rate_limited",
                    "image_stream_interrupted",
                    "image_poll_timeout",
                    "image_stream_timeout",
                    "network_error",
                    "upstream_connection_failed",
                    "upstream_connection_timeout",
                    "upstream_unavailable",
                }
                or (numeric_status is not None and numeric_status >= 500)
            )
            # Only count transient upstream pressure; permanent input/policy errors
            # should not freeze the whole generation pipeline.
            if transient:
                self._upstream_outcomes.append((now, False))

    def _prune_upstream_outcomes_unlocked(self) -> None:
        now = float(self._monotonic())
        ttl = float(self.UPSTREAM_ERROR_SAMPLE_TTL_SECONDS)
        self._upstream_outcomes = deque(
            ((ts, ok) for ts, ok in self._upstream_outcomes if (now - float(ts)) < ttl),
            maxlen=self.UPSTREAM_ERROR_WINDOW,
        )

    def upstream_error_rate(self) -> float:
        with self._lock:
            self._prune_upstream_outcomes_unlocked()
            if len(self._upstream_outcomes) < self.UPSTREAM_ERROR_MIN_SAMPLES:
                return 0.0
            failures = sum(1 for _ts, ok in self._upstream_outcomes if not ok)
            return failures / float(len(self._upstream_outcomes))

    def sample(self) -> ResourceSnapshot:
        """Read machine pressure, never raising.

        Occupancy decisions should go through ``evaluate()`` or ``allow_*()``.
        Machine probes use ``_probe_lock``; the occupancy latch uses ``_lock``.
        A caller that already has a snapshot can decide without waiting on disk,
        cgroup, or handle I/O. ``sample()`` is for diagnostics.
        A transient psutil/statvfs error must not turn into an HTTP 500 or stall
        the queue. Each probe degrades independently. CPU, memory, swap, thread,
        file-handle, and database pool failures report saturation so occupancy
        and generation walls close instead of looking idle. Disk is the same --
        a failed disk probe reports the pressure threshold rather than plenty of
        space.
        """
        return self._probe_machine()

    def _probe_machine(self) -> ResourceSnapshot:
        with self._probe_lock:
            now = float(self._monotonic())
            cached = self._probe_cache
            if cached is not None and (now - cached[0]) < self.CPU_OCCUPANCY_MIN_INTERVAL:
                refreshed = self._refresh_occupancy_fields(cached[1])
                self._probe_cache = (cached[0], refreshed)
                return refreshed
            snapshot = self._sample_unlocked()
            self._probe_cache = (float(self._monotonic()), snapshot)
            return snapshot

    def _refresh_occupancy_fields(self, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        available_memory, memory_limit = self._safe_memory_sample()
        swap_in_rate, swap_out_rate, swap_used_percent, swap_total_bytes = self._safe_swap_sample()
        overlay_cpu = self._safe_cpu_percent()
        pause = float(self.settings.occupancy_pause_percent)
        cached_cpu = max(0.0, min(100.0, float(snapshot.cpu_percent or 0.0)))
        cpu_percent = overlay_cpu if overlay_cpu >= pause or overlay_cpu >= cached_cpu else cached_cpu
        overlay_memory = self._memory_used_from_bytes(available_memory, memory_limit)
        cached_memory = self._memory_used_percent(snapshot)
        if overlay_memory >= pause or overlay_memory >= cached_memory:
            kept_available, kept_limit = available_memory, memory_limit
        else:
            kept_available, kept_limit = snapshot.available_memory_bytes, snapshot.memory_limit_bytes
        overlay_swap = swap_used_percent if int(swap_total_bytes or 0) > 0 else 0.0
        cached_swap = (
            float(snapshot.swap_used_percent or 0.0)
            if int(snapshot.swap_total_bytes or 0) > 0
            else 0.0
        )
        if overlay_swap >= pause or overlay_swap >= cached_swap:
            kept_swap_in, kept_swap_out = swap_in_rate, swap_out_rate
            kept_swap_used, kept_swap_total = swap_used_percent, swap_total_bytes
        else:
            kept_swap_in = snapshot.swap_in_bytes_per_second
            kept_swap_out = snapshot.swap_out_bytes_per_second
            kept_swap_used = snapshot.swap_used_percent
            kept_swap_total = snapshot.swap_total_bytes
        return replace(
            snapshot,
            cpu_percent=cpu_percent,
            available_memory_bytes=kept_available,
            memory_limit_bytes=kept_limit,
            swap_in_bytes_per_second=kept_swap_in,
            swap_out_bytes_per_second=kept_swap_out,
            swap_used_percent=kept_swap_used,
            swap_total_bytes=kept_swap_total,
            sampled_at=datetime.now(timezone.utc),
        )

    def _sample_unlocked(self) -> ResourceSnapshot:
        try:
            self.settings.artifact_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning({
                "event": "image_queue_resource_artifact_root_unavailable",
                "error": str(exc),
            })
        available_memory, memory_limit = self._safe_memory_sample()
        swap_in_rate, swap_out_rate, swap_used_percent, swap_total_bytes = self._safe_swap_sample()
        disk_free_bytes, disk_free_percent = self._safe_disk_sample()
        return ResourceSnapshot(
            cpu_percent=self._safe_cpu_percent(),
            available_memory_bytes=available_memory,
            memory_limit_bytes=memory_limit,
            swap_in_bytes_per_second=swap_in_rate,
            swap_out_bytes_per_second=swap_out_rate,
            thread_count=self._safe_thread_count(),
            file_handle_count=self._safe_file_handle_count(),
            database_pool_percent=self._safe_database_pool_percent(),
            disk_free_bytes=disk_free_bytes,
            disk_free_percent=disk_free_percent,
            sampled_at=datetime.now(timezone.utc),
            swap_used_percent=swap_used_percent,
            swap_total_bytes=swap_total_bytes,
        )

    def _resolve_snapshot(self, snapshot: ResourceSnapshot | None) -> ResourceSnapshot:
        if snapshot is None:
            return self._probe_machine()
        return snapshot

    @staticmethod
    def _cpu_times_idle_total(times: Any) -> tuple[float, float]:
        idle = float(getattr(times, "idle", 0.0) or 0.0) + float(getattr(times, "iowait", 0.0) or 0.0)
        named_total = 0.0
        named = False
        for name in (
            "user",
            "nice",
            "system",
            "idle",
            "iowait",
            "irq",
            "softirq",
            "steal",
            "interrupt",
            "dpc",
        ):
            if hasattr(times, name):
                named = True
                named_total += float(getattr(times, name, 0.0) or 0.0)
        if named:
            total = named_total
        else:
            try:
                total = float(sum(float(item or 0.0) for item in times))
            except TypeError:
                total = idle
            guest = float(getattr(times, "guest", 0.0) or 0.0) + float(
                getattr(times, "guest_nice", 0.0) or 0.0
            )
            if guest > 0.0:
                total = max(idle, total - guest)
        return idle, total if total > 0 else idle

    def _host_cpu_percent(self) -> float:
        times = psutil.cpu_times()
        idle, total = self._cpu_times_idle_total(times)
        now = float(self._monotonic())
        previous = self._host_cpu_times_sample
        pause = float(self.settings.occupancy_pause_percent)
        try:
            bootstrap = max(0.0, min(100.0, float(psutil.cpu_percent(interval=None))))
        except Exception:
            bootstrap = 0.0 if previous is None else float(previous[3])
        if previous is None:
            self._host_cpu_times_sample = (idle, total, now, bootstrap)
            return bootstrap
        last_idle, last_total, last_now, last_percent = previous
        elapsed = now - last_now
        dt_total = total - last_total
        dt_idle = idle - last_idle
        if elapsed <= 0 or dt_total <= 0:
            # Same jiffy / frozen counters: keep the last times-window percent.
            # Non-blocking cpu_percent(interval=None) is a 1ms spike and must
            # not overwrite the occupancy sample or start the 2.5s high timer.
            return last_percent
        percent = max(0.0, min(100.0, (1.0 - (dt_idle / dt_total)) * 100.0))
        if elapsed < self.CPU_OCCUPANCY_MIN_INTERVAL and percent < pause and percent < last_percent:
            return last_percent
        self._host_cpu_times_sample = (idle, total, now, percent)
        return percent

    def _safe_cpu_percent(self) -> float:
        try:
            return self._container_cpu_percent(self._host_cpu_percent())
        except Exception as exc:
            self._log_probe_failure("cpu", exc)
            return 100.0

    def _safe_memory_sample(self) -> tuple[int, int]:
        try:
            return self._memory_sample(psutil.virtual_memory())
        except Exception as exc:
            self._log_probe_failure("memory", exc)
            return 0, max(1, int(FALLBACK_AVAILABLE_MEMORY_BYTES))

    def _safe_swap_sample(self) -> tuple[int, int, float, int]:
        try:
            swap = psutil.swap_memory()
            in_rate, out_rate = self._swap_rates(swap)
            total = int(getattr(swap, "total", 0) or 0)
            used = int(getattr(swap, "used", 0) or 0)
            percent = float(getattr(swap, "percent", 0) or 0)
            if total <= 0:
                return in_rate, out_rate, 0.0, 0
            if percent <= 0 and used > 0:
                percent = used / total * 100.0
            return in_rate, out_rate, max(0.0, min(100.0, percent)), total
        except Exception as exc:
            self._log_probe_failure("swap", exc)
            return 0, 0, 100.0, 1

    def _safe_thread_count(self) -> int:
        try:
            return int(psutil.Process().num_threads())
        except Exception as exc:
            self._log_probe_failure("thread_count", exc)
            return max(1, int(getattr(self.settings, "absolute_guard", 1) or 1))

    def _safe_file_handle_count(self) -> int:
        try:
            return self._file_handle_count(psutil.Process())
        except Exception as exc:
            self._log_probe_failure("file_handles", exc)
            return int(self._file_handle_guard())

    def _safe_database_pool_percent(self) -> float:
        try:
            return self._database_pool_percent()
        except Exception as exc:
            self._log_probe_failure("database_pool", exc)
            return 100.0

    def _safe_disk_sample(self) -> tuple[int, float]:
        try:
            disk = shutil.disk_usage(self.settings.artifact_root.resolve())
        except Exception as exc:
            self._log_probe_failure("disk", exc)
            # Refuse to claim free space we could not measure: report exactly the
            # pressure threshold so submissions pause instead of filling the disk.
            return 0, 0.0
        total = int(disk.total)
        return int(disk.free), (float(disk.free) / float(total) * 100.0) if total else 0.0

    def _log_probe_failure(self, probe: str, exc: BaseException) -> None:
        logger.warning({
            "event": "image_queue_resource_probe_failed",
            "probe": probe,
            "error": str(exc),
        })

    def _disk_pressure(self, snapshot: ResourceSnapshot) -> bool:
        return snapshot.disk_free_bytes < 5 * 1024**3 or snapshot.disk_free_percent < 5.0

    @staticmethod
    def _memory_used_from_bytes(available_bytes: int, limit_bytes: int) -> float:
        if int(limit_bytes or 0) <= 0:
            return 0.0
        used = max(0, int(limit_bytes) - max(0, int(available_bytes or 0)))
        return max(0.0, min(100.0, float(used) / float(limit_bytes) * 100.0))

    @classmethod
    def _memory_used_percent(cls, snapshot: ResourceSnapshot) -> float:
        return cls._memory_used_from_bytes(
            snapshot.available_memory_bytes,
            snapshot.memory_limit_bytes,
        )

    def _memory_pressure(self, snapshot: ResourceSnapshot) -> bool:
        return snapshot.memory_limit_bytes > 0 and snapshot.available_memory_bytes < max(
            512 * 1024**2,
            int(snapshot.memory_limit_bytes * 0.05),
        )

    def _occupancy_percentages(self, snapshot: ResourceSnapshot) -> tuple[float, float, float]:
        cpu = max(0.0, min(100.0, float(snapshot.cpu_percent or 0.0)))
        memory = self._memory_used_percent(snapshot)
        swap = 0.0
        if int(getattr(snapshot, "swap_total_bytes", 0) or 0) > 0:
            swap = max(0.0, min(100.0, float(getattr(snapshot, "swap_used_percent", 0.0) or 0.0)))
        return cpu, memory, swap

    def _occupancy_reason_from_values(self, cpu: float, memory: float, swap: float) -> str:
        pause = float(self.settings.occupancy_pause_percent)
        if cpu >= pause:
            return "resource_cpu"
        if memory >= pause:
            return "resource_memory"
        if swap >= pause:
            return "resource_swap"
        return "resource_occupancy"

    def _occupancy_reason(self, snapshot: ResourceSnapshot) -> str:
        cpu, memory, swap = self._occupancy_percentages(snapshot)
        return self._occupancy_reason_from_values(cpu, memory, swap)

    def _stabilize_occupancy_cpu(self, raw: float) -> float:
        now = float(self._monotonic())
        clamped = max(0.0, min(100.0, float(raw)))
        previous = self._occupancy_cpu_sample
        pause = float(self.settings.occupancy_pause_percent)
        if clamped >= pause:
            self._occupancy_cpu_sample = (clamped, now)
            return clamped
        if (
            not self._occupancy_paused
            and previous is not None
            and previous[0] >= pause
            and (now - previous[1]) < self.CPU_OCCUPANCY_MIN_INTERVAL
        ):
            return previous[0]
        self._occupancy_cpu_sample = (clamped, now)
        return clamped

    def _update_occupancy_gate(self, snapshot: ResourceSnapshot) -> str:
        now = float(self._monotonic())
        hold = max(0.1, float(self.settings.occupancy_hold_seconds))
        pause = float(self.settings.occupancy_pause_percent)
        resume = float(self.settings.occupancy_resume_percent)
        cpu, memory, swap = self._occupancy_percentages(snapshot)
        cpu = self._stabilize_occupancy_cpu(cpu)
        high = cpu >= pause or memory >= pause or swap >= pause
        low = cpu < resume and memory < resume and swap < resume
        if high:
            self._occupancy_low_since = None
            if self._occupancy_high_since is None:
                self._occupancy_high_since = now
            if not self._occupancy_paused and (now - self._occupancy_high_since) >= hold:
                self._occupancy_paused = True
                self._occupancy_pause_reason = self._occupancy_reason_from_values(cpu, memory, swap)
        elif self._occupancy_paused:
            self._occupancy_high_since = None
            if low:
                if self._occupancy_low_since is None:
                    self._occupancy_low_since = now
                if (now - self._occupancy_low_since) >= hold:
                    self._occupancy_paused = False
                    self._occupancy_pause_reason = ""
                    self._occupancy_low_since = None
            else:
                self._occupancy_low_since = None
        elif low:
            self._occupancy_high_since = None
            self._occupancy_low_since = None
        else:
            # 80–90% band: keep the high timer so a brief dip does not cancel the latch.
            self._occupancy_low_since = None
        if self._occupancy_paused:
            return self._occupancy_pause_reason or self._occupancy_reason_from_values(cpu, memory, swap)
        return ""

    def _hard_resource_wall(
        self,
        snapshot: ResourceSnapshot,
        *,
        include_upstream: bool = True,
    ) -> str:
        if self._disk_pressure(snapshot):
            return "resource_disk"
        if snapshot.database_pool_percent >= 85.0:
            return "resource_database_pool"
        if self._memory_pressure(snapshot):
            return "resource_memory"
        if snapshot.file_handle_count >= self._file_handle_guard():
            return "resource_file_handles"
        if snapshot.thread_count >= self.settings.absolute_guard:
            return "resource_threads"
        if include_upstream and self.upstream_error_rate() >= self.UPSTREAM_ERROR_RATE_THRESHOLD:
            return "resource_upstream_errors"
        return ""

    def _allow_recovery_unlocked(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        if self._disk_pressure(snapshot):
            return ResourceDecision(False, "resource_disk", 0)
        if self._memory_pressure(snapshot):
            return ResourceDecision(False, "resource_memory", 0)
        if snapshot.database_pool_percent >= 95.0:
            return ResourceDecision(False, "resource_database_pool", 0)
        if snapshot.file_handle_count >= self._file_handle_guard():
            return ResourceDecision(False, "resource_file_handles", 0)
        if snapshot.thread_count >= self.settings.absolute_guard:
            return ResourceDecision(False, "resource_threads", 0)
        return ResourceDecision(True, "", max(1, self.settings.absolute_guard))

    def _allow_new_generation_unlocked(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        occupancy_reason = self._update_occupancy_gate(snapshot)
        if occupancy_reason:
            return ResourceDecision(False, occupancy_reason, 0)
        hard_reason = self._hard_resource_wall(snapshot)
        if hard_reason:
            return ResourceDecision(False, hard_reason, 0)
        return ResourceDecision(True, "", max(1, int(self.settings.generation_concurrency_limit)))

    def _allow_new_registration_unlocked(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        occupancy_reason = self._update_occupancy_gate(snapshot)
        if occupancy_reason:
            return ResourceDecision(False, occupancy_reason, 0)
        return ResourceDecision(True, "", 1)

    def _allow_new_submission_unlocked(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        if self._disk_pressure(snapshot):
            return ResourceDecision(False, "resource_disk", 0)
        occupancy_reason = self._update_occupancy_gate(snapshot)
        if occupancy_reason:
            return ResourceDecision(False, occupancy_reason, 0)
        hard_reason = self._hard_resource_wall(snapshot, include_upstream=False)
        if hard_reason:
            return ResourceDecision(False, hard_reason, 0)
        return ResourceDecision(True, "", 1)

    def evaluate(
        self,
        snapshot: ResourceSnapshot | None = None,
    ) -> tuple[ResourceSnapshot, ResourceDecision, ResourceDecision]:
        resolved = self._resolve_snapshot(snapshot)
        with self._lock:
            generation = self._allow_new_generation_unlocked(resolved)
            recovery = self._allow_recovery_unlocked(resolved)
            return resolved, generation, recovery

    def allow_recovery(self, snapshot: ResourceSnapshot | None = None) -> ResourceDecision:
        """Recovery/saving may continue under CPU pressure; only hard resource walls block it."""
        resolved = self._resolve_snapshot(snapshot)
        with self._lock:
            return self._allow_recovery_unlocked(resolved)

    def allow_new_generation(self, snapshot: ResourceSnapshot | None = None) -> ResourceDecision:
        resolved = self._resolve_snapshot(snapshot)
        with self._lock:
            return self._allow_new_generation_unlocked(resolved)

    def allow_new_registration(self, snapshot: ResourceSnapshot | None = None) -> ResourceDecision:
        resolved = self._resolve_snapshot(snapshot)
        with self._lock:
            return self._allow_new_registration_unlocked(resolved)

    def allow_new_submission(self, snapshot: ResourceSnapshot | None = None) -> ResourceDecision:
        resolved = self._resolve_snapshot(snapshot)
        with self._lock:
            return self._allow_new_submission_unlocked(resolved)
