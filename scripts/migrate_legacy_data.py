#!/usr/bin/env python3
"""Import supported chatgpt2api data into gptimage2api exactly once.

The legacy project remains untouched. Existing destination files are never
silently overwritten with different bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from services.application_database import resolve_database_url
from services.config import DATA_DIR
from services.register_config_store import (
    DatabaseRegisterConfigStore,
    validate_register_config_for_storage,
)
from services.storage.database_storage import DatabaseStorageBackend


_STATE_FILES = (
    "outlook_token_used.json",
    "register_core_results_pending.json",
    "remail_dead_mailboxes.json",
)
_MEDIA_DIRECTORIES = ("images", "files")
_MEDIA_FILES = ("image_tags.json",)


def _load_items(path: Path, *, collection: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError(f"legacy migration does not accept symlinked files: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON") from exc
    if isinstance(raw, dict):
        raw = raw.get("items", raw.get(collection, []))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array or an items object")
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError(f"{path} must contain only JSON objects")
    return list(raw)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_once(source: Path, target: Path) -> bool:
    _validate_file_copy(source, target)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _validate_file_copy(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"legacy migration does not accept symlinked files: {source}")
    if not source.is_file():
        raise ValueError(f"legacy migration source is not a regular file: {source}")
    if target.exists():
        if target.is_symlink():
            raise ValueError(f"legacy migration does not overwrite symlinked files: {target}")
        if not target.is_file():
            raise ValueError(f"legacy migration destination is not a file: {target}")
        if _sha256(source) != _sha256(target):
            raise ValueError(
                f"legacy migration conflict: destination differs from source: {target}"
            )


def _copy_tree_once(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"legacy migration source is not a regular directory: {source}")
    copied = 0
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"legacy migration does not accept symlinked files: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError(f"legacy migration source is not a regular file: {item}")
        if _copy_file_once(item, target / item.relative_to(source)):
            copied += 1
    return copied


def _validate_tree_copy(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"legacy migration source is not a regular directory: {source}")
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"legacy migration does not accept symlinked files: {item}")
        if item.is_dir():
            continue
        _validate_file_copy(item, target / item.relative_to(source))


def _validate_accounts(accounts: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, account in enumerate(accounts, start=1):
        token = str(account.get("access_token") or "").strip()
        if not token:
            raise ValueError(f"accounts.json item {index} requires a non-empty access_token")
        if token in seen:
            raise ValueError("accounts.json contains duplicate access_token values")
        seen.add(token)


def _normalize_auth_keys(auth_keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(auth_keys, start=1):
        key_id = str(item.get("key_id") or item.get("id") or "").strip()
        if not key_id:
            raise ValueError(f"auth_keys.json item {index} requires a non-empty key_id")
        if key_id in seen:
            raise ValueError("auth_keys.json contains duplicate key_id values")
        seen.add(key_id)
        normalized.append({**item, "id": key_id, "key_id": key_id})
    return normalized


def _merge_by_identity(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    field: str,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing:
        key = str(item.get(field) or "").strip()
        if key:
            merged[key] = item
    for item in incoming:
        merged[str(item[field]).strip()] = item
    return list(merged.values())


def _copy_runtime_state(source: Path, target_data_dir: Path) -> int:
    copied = 0
    for name in _STATE_FILES:
        candidate = source / name
        if candidate.exists() and _copy_file_once(candidate, target_data_dir / name):
            copied += 1
    return copied


def _copy_media(source: Path, target_data_dir: Path) -> int:
    copied = 0
    for name in _MEDIA_FILES:
        candidate = source / name
        if candidate.exists() and _copy_file_once(candidate, target_data_dir / name):
            copied += 1
    for name in _MEDIA_DIRECTORIES:
        copied += _copy_tree_once(source / name, target_data_dir / name)
    return copied


def _load_register_config(source: Path) -> dict[str, Any] | None:
    legacy_path = source / "register.json"
    if not legacy_path.exists():
        return None
    if legacy_path.is_symlink():
        raise ValueError(f"legacy migration does not accept symlinked files: {legacy_path}")
    try:
        value = json.loads(legacy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{legacy_path} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{legacy_path} must contain a JSON object")
    return value


def _validate_json_file(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"legacy migration source is not a regular file: {path}")
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON") from exc


def _validate_runtime_copy_plan(source: Path, target_data_dir: Path) -> None:
    for name in _STATE_FILES + _MEDIA_FILES:
        candidate = source / name
        _validate_json_file(candidate)
        if candidate.exists():
            _validate_file_copy(candidate, target_data_dir / name)
    for name in _MEDIA_DIRECTORIES:
        _validate_tree_copy(source / name, target_data_dir / name)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sqlite_database_path(database_url: str) -> Path | None:
    """Resolve a file-backed SQLite URL without treating PostgreSQL as a path."""
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise ValueError(f"invalid Application Database URL: {exc}") from exc
    if parsed.drivername != "sqlite":
        return None
    database = str(parsed.database or "").strip()
    if not database or database == ":memory:":
        return None
    # SQLAlchemy may expose an absolute Windows path as /C:/... when it came
    # from a URL. Normalize that form before Path.resolve().
    if re.match(r"^/[A-Za-z]:[\\/]", database):
        database = database[1:]
    return Path(database).expanduser().resolve()


def migrate_legacy_data(
    source_data_dir: str | Path,
    *,
    database_url: str | None = None,
    target_data_dir: str | Path | None = None,
) -> dict[str, int]:
    """Migrate accounts, keys, registration state, and optional media.

    Active image queue tasks, leases, workers, and cluster state are
    deliberately excluded. They are live coordination state, not portable data.
    """
    source = Path(source_data_dir).expanduser().resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"legacy data directory must be a regular directory: {source}")
    target = str(database_url or resolve_database_url()).strip()
    target_data = Path(target_data_dir or DATA_DIR).expanduser().resolve()
    if _is_within(target_data, source):
        raise ValueError("target_data_dir must not be inside the legacy source directory")
    database_path = _sqlite_database_path(target)
    if database_path is not None and _is_within(database_path, source):
        raise ValueError("Application Database must not be inside the legacy source directory")

    accounts = _load_items(source / "accounts.json", collection="accounts")
    auth_keys = _normalize_auth_keys(
        _load_items(source / "auth_keys.json", collection="auth_keys")
    )
    _validate_accounts(accounts)
    register_config_data = _load_register_config(source)
    _validate_runtime_copy_plan(source, target_data)

    register_config_store = DatabaseRegisterConfigStore(target)
    if register_config_data and not register_config_store.has_config():
        # Fail before account/key writes when the config cannot be encrypted.
        # The write below uses the same sanitizer and encoder.
        validate_register_config_for_storage(register_config_data)

    # Account/key input and copy destinations have been validated before the
    # Application Database mutation. Each repository write is transactional
    # and keyed by the upstream identities.
    backend = DatabaseStorageBackend(target)
    account_rows = _merge_by_identity(
        backend.load_accounts(), accounts, field="access_token"
    )
    auth_key_rows = _merge_by_identity(
        backend.load_auth_keys(), auth_keys, field="id"
    )
    backend.replace_accounts(account_rows)
    backend.replace_auth_keys(auth_key_rows)
    register_config = int(
        bool(register_config_data)
        and register_config_store.import_legacy_config_once(register_config_data)
    )
    state_files = _copy_runtime_state(source, target_data)
    media_files = _copy_media(source, target_data)

    return {
        "accounts": len(accounts),
        "auth_keys": len(auth_keys),
        "register_config": register_config,
        "state_files": state_files,
        "media_files": media_files,
        "total_accounts": len(account_rows),
        "total_auth_keys": len(auth_key_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_data_dir", type=Path)
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--target-data-dir",
        type=Path,
        default=DATA_DIR,
        help="gptimage2api data directory for registration state and media",
    )
    args = parser.parse_args()
    result = migrate_legacy_data(
        args.source_data_dir,
        database_url=args.database_url,
        target_data_dir=args.target_data_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
