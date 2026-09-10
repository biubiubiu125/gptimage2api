from __future__ import annotations

import os


DEFAULT_THREAD_TOKENS = 120


def _env_candidates(name: str, legacy_names: tuple[str, ...]) -> tuple[str, ...]:
    candidates = [str(name)]
    candidates.extend(str(item) for item in legacy_names if str(item) not in candidates)
    if str(name).startswith("GPTIMAGE2API_"):
        legacy_name = "CHATGPT2API_" + str(name)[len("GPTIMAGE2API_"):]
        if legacy_name not in candidates:
            candidates.append(legacy_name)
    return tuple(candidates)


def env_value(name: str, *legacy_names: str, default: str = "") -> str:
    """Read the new variable first, then legacy compatibility names."""
    for candidate in _env_candidates(name, tuple(legacy_names)):
        value = os.getenv(candidate)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(env_value(name, default=str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value
