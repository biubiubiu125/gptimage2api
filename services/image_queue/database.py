from __future__ import annotations

from contextlib import contextmanager
import json
from threading import RLock
from typing import Iterator
from uuid import UUID, uuid4

from sqlalchemy import Engine, UniqueConstraint, create_engine, func, inspect, select, text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from services.image_queue.models import Base, ImageJob, ImageQueueSchemaMigration, ImageTask, ImageTaskArtifact, utc_now
from services.image_queue.settings import ImageQueueConfigurationError, ImageQueueSettings
from services.database_url import IMAGE_QUEUE_DATABASE_ROLE, ensure_database_role_marker


SCHEMA_VERSION = 10
ADVISORY_LOCK_KEY = "gptimage2api-image-queue-v1"


class ImageQueueUnavailableError(RuntimeError):
    code = "image_queue_unavailable"


def _constraint_columns(columns) -> tuple[str, ...]:
    return tuple(str(column.name) for column in columns)


def _quote_name(connection, value: str) -> str:
    return connection.dialect.identifier_preparer.quote(value)


def _table_sql(connection, table) -> str:
    return connection.dialect.identifier_preparer.format_table(table)


def _unique_sets(inspector, table_name: str) -> set[tuple[str, ...]]:
    unique_sets = {
        tuple(str(column) for column in constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("column_names")
    }
    unique_sets.update({
        tuple(str(column) for column in index.get("column_names") or ())
        for index in inspector.get_indexes(table_name)
        if index.get("unique") and index.get("column_names")
    })
    return unique_sets


def _index_signatures(
    inspector,
    table_name: str,
) -> set[tuple[tuple[str, ...], bool]]:
    return {
        (
            tuple(str(column) for column in index.get("column_names") or ()),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes(table_name)
        if index.get("column_names")
    }


def _type_family(value) -> str:
    name = value.__class__.__name__.casefold()
    if "json" in name:
        return "json"
    if "uuid" in name:
        return "uuid"
    if "bigint" in name:
        return "bigint"
    if "integer" in name or name in {"int", "int4"}:
        return "integer"
    if "bool" in name:
        return "boolean"
    if "datetime" in name or "timestamp" in name:
        return "datetime"
    if "text" in name:
        return "text"
    if "char" in name or "string" in name or "varchar" in name:
        return "string"
    if "binary" in name or "blob" in name or "bytea" in name:
        return "binary"
    return name


def _types_compatible(expected, actual) -> bool:
    expected_family = _type_family(expected)
    actual_family = _type_family(actual)
    if expected_family == actual_family:
        return True
    if expected_family == "json" and actual_family in {"json", "jsonb"}:
        return True
    # SQLAlchemy renders Uuid as CHAR(32) on SQLite.
    if expected_family == "uuid" and actual_family == "string":
        return int(getattr(actual, "length", 0) or 0) == 32
    return False


_NO_DEFAULT = object()


def _migration_default_value(table_name: str, column_name: str, column=None) -> object:
    if column_name in {
        "id",
        "account_id",
        "task_id",
        "job_id",
        "worker_id",
        "file_sha256",
        "lease_token",
    }:
        return _NO_DEFAULT
    if table_name == "image_jobs" and column_name == "quota_accounting_error":
        return None
    if table_name == "image_jobs" and column_name == "ordinal":
        return _NO_DEFAULT
    if column_name in {"request_payload", "result_payload", "stage_timings", "event_data", "summary", "resource_snapshot"}:
        return {}
    if column_name in {"image_urls", "file_ids", "sediment_ids"}:
        return []
    if column_name in {"cancel_requested", "quota_consumed", "quota_accounted"}:
        return False
    if column_name in {"required_jobs"}:
        return 1
    if column_name in {"version"}:
        return 1
    if column_name in {"status"}:
        return "queued"
    if column_name in {"stage"}:
        return "queued"
    if column_name in {"delivery_status"}:
        return "pending"
    if column_name.endswith("_at") or column_name in {"created_at", "updated_at"}:
        return utc_now()
    if column_name in {
        "attempt",
        "generate_attempts",
        "download_attempts",
        "save_attempts",
        "lease_version",
        "quota_accounting_attempts",
        "succeeded_jobs",
        "failed_jobs",
        "effective_concurrency",
        "byte_size",
        "width",
        "height",
    }:
        return 0
    if column_name in {"effective_prompt", "original_prompt", "owner_key", "request_hash", "task_type", "public_model"}:
        return {
            "owner_key": "legacy-migrated",
            "request_hash": "legacy_migrated",
            "task_type": "generation",
            "public_model": "gpt-image-2",
        }.get(column_name, "")
    if column is not None:
        family = _type_family(column.type)
        if family in {"integer", "bigint"}:
            return 0
        if family == "boolean":
            return False
        if family == "json":
            return {}
        if family == "datetime":
            return utc_now()
        if family == "binary":
            return b""
        if family == "uuid":
            return _NO_DEFAULT
    return ""


def _coerce_migration_value(column, value: object) -> object:
    if value is None:
        return None
    family = _type_family(column.type)
    if family == "uuid":
        if isinstance(value, UUID):
            return value
        raw = str(value).strip()
        try:
            return UUID(raw)
        except ValueError:
            try:
                return UUID(hex=raw)
            except ValueError:
                return uuid4()
    if family == "json" and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    if family == "boolean" and isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if family == "datetime" and isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utc_now()
    if family in {"integer", "bigint"}:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if raw == "":
                return 0
            try:
                return int(raw)
            except ValueError:
                return 0
        if isinstance(value, float):
            return int(value)
        return 0
    return value


def _migration_foreign_key_target(column) -> tuple[str, str] | None:
    foreign_keys = list(getattr(column, "foreign_keys", ()) or ())
    if not foreign_keys:
        return None
    target = str(foreign_keys[0].target_fullname or "")
    table_name, separator, column_name = target.rpartition(".")
    if not separator or not table_name or not column_name:
        return None
    return table_name, column_name


def _legacy_migration_value(
    table_name: str,
    column_name: str,
    row: dict[str, object],
) -> object:
    if column_name in row and row[column_name] is not None:
        return row[column_name]
    aliases = {
        ("image_tasks", "owner_key"): "owner_id",
        ("image_tasks", "public_model"): "model",
    }
    alias = aliases.get((table_name, column_name))
    if alias and row.get(alias) not in (None, ""):
        return row[alias]
    if table_name == "image_tasks":
        if column_name == "client_task_id":
            return row.get("id")
        if column_name == "idempotency_key":
            return f"migrated:{row.get('id')}"
        if column_name == "task_type":
            return "edit" if str(row.get("mode") or "").strip().lower() == "edit" else "generation"
        if column_name == "required_jobs":
            return row.get("n") or 1
    return _migration_default_value(table_name, column_name)


def _foreign_key_sets(inspector, table_name: str) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    result: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for foreign_key in inspector.get_foreign_keys(table_name):
        constrained = tuple(str(item) for item in foreign_key.get("constrained_columns") or ())
        referred_table = str(foreign_key.get("referred_table") or "")
        referred = tuple(str(item) for item in foreign_key.get("referred_columns") or ())
        if constrained and referred_table and referred:
            result.add((constrained, referred_table, referred))
    return result


def _expected_foreign_key_sets(table) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    result: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for constraint in table.foreign_key_constraints:
        constrained = tuple(str(column.parent.name) for column in constraint.elements)
        referred_table = str(constraint.elements[0].target_fullname.rsplit(".", 1)[0])
        referred = tuple(str(column.column.name) for column in constraint.elements)
        if constrained and referred_table and referred:
            result.add((constrained, referred_table, referred))
    return result


def _foreign_key_name(table, signature: tuple[tuple[str, ...], str, tuple[str, ...]]) -> str:
    constrained, referred_table, _referred = signature
    for constraint in table.foreign_key_constraints:
        current = (
            tuple(str(column.parent.name) for column in constraint.elements),
            str(constraint.elements[0].target_fullname.rsplit(".", 1)[0]),
            tuple(str(column.column.name) for column in constraint.elements),
        )
        if current == signature and constraint.name:
            return str(constraint.name)
    return f"fk_{table.name}_{'_'.join(constrained)}_{referred_table}"


def _missing_foreign_keys(inspector, table) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return _expected_foreign_key_sets(table) - _foreign_key_sets(inspector, table.name)


def _table_needs_sqlite_rebuild(inspector, table) -> bool:
    actual_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(table.name)
    }
    for column in table.columns:
        actual = actual_columns.get(column.name)
        if actual is None:
            return True
        if not column.nullable and bool(actual.get("nullable", True)):
            return True
    return bool(_missing_foreign_keys(inspector, table))


def _sqlite_migrated_rows(
    table,
    rows: list[dict[str, object]],
    id_maps: dict[tuple[str, str], dict[str, UUID]],
) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {}
        for column in table.columns:
            value = _legacy_migration_value(table.name, column.name, row)
            if value is _NO_DEFAULT:
                value = None
            if value is None and not column.nullable:
                value = _migration_default_value(table.name, column.name, column)
            if value is _NO_DEFAULT:
                value = None
            raw_key = str(value).strip() if value is not None else ""
            target = _migration_foreign_key_target(column)
            if target and raw_key:
                mapped = id_maps.get(target, {}).get(raw_key)
                if mapped is not None:
                    value = mapped
            item[column.name] = _coerce_migration_value(column, value)
            if column.primary_key and _type_family(column.type) == "uuid" and raw_key:
                id_maps.setdefault((table.name, column.name), {})[raw_key] = item[column.name]
        values.append(item)
    return values


def _rebuild_sqlite_tables(connection, tables) -> None:
    """Rebuild a related SQLite table set without cascading away child rows.

    SQLite executes ``ON DELETE CASCADE`` while a referenced table is dropped.
    Rebuilding one parent at a time therefore loses rows from existing child
    tables.  Snapshot the whole foreign-key component, drop children first,
    recreate parents first, and restore rows after UUID coercion.
    """
    ordered_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name in {item.name for item in tables}
    ]
    if not ordered_tables:
        return

    snapshots = {
        table.name: [
            dict(row)
            for row in connection.execute(
                text(f"SELECT * FROM {_table_sql(connection, table)}")
            ).mappings().all()
        ]
        for table in ordered_tables
    }
    table_by_name = {table.name: table for table in ordered_tables}
    for table in reversed(ordered_tables):
        connection.execute(text(f"DROP TABLE {_table_sql(connection, table)}"))
    for table in ordered_tables:
        table.create(connection)

    id_maps: dict[tuple[str, str], dict[str, UUID]] = {}
    for table in ordered_tables:
        values = _sqlite_migrated_rows(table, snapshots[table.name], id_maps)
        if values:
            connection.execute(table.insert(), values)


def _add_missing_column(connection, table, column) -> None:
    statement = (
        f"ALTER TABLE {_table_sql(connection, table)} "
        f"ADD COLUMN {_quote_name(connection, column.name)} {column.type.compile(dialect=connection.dialect)}"
    )
    connection.execute(text(statement))


def _create_unique_index(connection, table, name: str, column_names: tuple[str, ...]) -> None:
    columns = ", ".join(_quote_name(connection, column) for column in column_names)
    statement = (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote_name(connection, name)} "
        f"ON {_table_sql(connection, table)} ({columns})"
    )
    connection.execute(text(statement))


def _set_null_default(connection, table, column_name: str, value: object) -> None:
    column = table.c[column_name]
    connection.execute(
        table.update()
        .where(column.is_(None))
        .values({column_name: _coerce_migration_value(column, value)})
    )


def _update_sql(connection, statement: str, parameters: dict[str, object] | None = None) -> None:
    connection.execute(text(statement), parameters or {})


def _backfill_task_defaults(connection, columns: set[str]) -> None:
    table_name = _table_sql(connection, ImageTask.__table__)
    now = utc_now()
    if "owner_id" in columns:
        _update_sql(
            connection,
            f"UPDATE {table_name} SET owner_key = owner_id "
            "WHERE owner_key IS NULL AND owner_id IS NOT NULL AND owner_id <> ''",
        )
    _set_null_default(connection, ImageTask.__table__, "owner_key", "legacy-migrated")
    _update_sql(
        connection,
        f"UPDATE {table_name} SET client_task_id = CAST(id AS TEXT) "
        "WHERE client_task_id IS NULL OR client_task_id = ''",
    )
    _update_sql(
        connection,
        f"UPDATE {table_name} SET idempotency_key = 'migrated:' || CAST(id AS TEXT) "
        "WHERE idempotency_key IS NULL OR idempotency_key = ''",
    )
    if "mode" in columns:
        _update_sql(
            connection,
            f"UPDATE {table_name} SET task_type = CASE WHEN mode = 'edit' THEN 'edit' ELSE 'generation' END "
            "WHERE task_type IS NULL OR task_type = ''",
        )
    _set_null_default(connection, ImageTask.__table__, "task_type", "generation")
    if "model" in columns:
        _update_sql(
            connection,
            f"UPDATE {table_name} SET public_model = model "
            "WHERE (public_model IS NULL OR public_model = '') AND model IS NOT NULL AND model <> ''",
        )
    _set_null_default(connection, ImageTask.__table__, "public_model", "gpt-image-2")
    _set_null_default(connection, ImageTask.__table__, "request_hash", "legacy_migrated")
    _set_null_default(connection, ImageTask.__table__, "original_prompt", "")
    _set_null_default(connection, ImageTask.__table__, "effective_prompt", "")
    _set_null_default(connection, ImageTask.__table__, "request_payload", {})
    if "n" in columns:
        _update_sql(
            connection,
            f"UPDATE {table_name} SET required_jobs = n "
            "WHERE required_jobs IS NULL AND n IS NOT NULL AND n > 0",
        )
    _set_null_default(connection, ImageTask.__table__, "required_jobs", 1)
    _update_sql(connection, f"UPDATE {table_name} SET status = 'failed' WHERE status = 'error'")
    _set_null_default(connection, ImageTask.__table__, "succeeded_jobs", 0)
    _set_null_default(connection, ImageTask.__table__, "failed_jobs", 0)
    _update_sql(
        connection,
        f"UPDATE {table_name} SET succeeded_jobs = required_jobs "
        "WHERE status = 'success' AND succeeded_jobs = 0",
    )
    _update_sql(
        connection,
        f"UPDATE {table_name} SET failed_jobs = required_jobs "
        "WHERE status IN ('failed', 'canceled') AND failed_jobs = 0",
    )
    _set_null_default(connection, ImageTask.__table__, "delivery_status", "pending")
    _set_null_default(connection, ImageTask.__table__, "cancel_requested", False)
    _set_null_default(connection, ImageTask.__table__, "created_at", now)
    _update_sql(
        connection,
        f"UPDATE {table_name} SET queued_at = created_at WHERE queued_at IS NULL",
    )
    _set_null_default(connection, ImageTask.__table__, "updated_at", now)
    _update_sql(
        connection,
        f"UPDATE {table_name} SET started_at = created_at WHERE status = 'success' AND started_at IS NULL",
    )
    _update_sql(
        connection,
        f"UPDATE {table_name} SET completed_at = updated_at "
        "WHERE status IN ('success', 'failed', 'canceled') AND completed_at IS NULL",
    )
    _set_null_default(connection, ImageTask.__table__, "version", 1)


def _backfill_job_ordinals(connection) -> None:
    table_name = _table_sql(connection, ImageJob.__table__)
    rows = connection.execute(text(
        f"SELECT id, task_id FROM {table_name} WHERE ordinal IS NULL ORDER BY task_id, id"
    )).mappings().all()
    counters: dict[str, int] = {}
    for row in rows:
        task_key = str(row.get("task_id") or row.get("id") or "")
        counters[task_key] = counters.get(task_key, 0) + 1
        connection.execute(
            text(f"UPDATE {table_name} SET ordinal = :ordinal WHERE id = :id"),
            {"ordinal": counters[task_key], "id": row["id"]},
        )


def _backfill_job_defaults(connection) -> None:
    table_name = _table_sql(connection, ImageJob.__table__)
    now = utc_now()
    _backfill_job_ordinals(connection)
    _set_null_default(connection, ImageJob.__table__, "generate_attempts", 0)
    _set_null_default(connection, ImageJob.__table__, "download_attempts", 0)
    _set_null_default(connection, ImageJob.__table__, "save_attempts", 0)
    _set_null_default(connection, ImageJob.__table__, "available_at", now)
    _set_null_default(connection, ImageJob.__table__, "lease_version", 0)
    _set_null_default(connection, ImageJob.__table__, "image_urls", [])
    _set_null_default(connection, ImageJob.__table__, "file_ids", [])
    _set_null_default(connection, ImageJob.__table__, "sediment_ids", [])
    _set_null_default(connection, ImageJob.__table__, "quota_consumed", False)
    _set_null_default(connection, ImageJob.__table__, "quota_accounted", False)
    _set_null_default(connection, ImageJob.__table__, "result_payload", {})
    _set_null_default(connection, ImageJob.__table__, "stage_timings", {})
    _set_null_default(connection, ImageJob.__table__, "created_at", now)
    _set_null_default(connection, ImageJob.__table__, "updated_at", now)
    _update_sql(connection, f"UPDATE {table_name} SET status = 'failed' WHERE status = 'error'")
    _update_sql(connection, f"UPDATE {table_name} SET stage = 'failed' WHERE stage = 'error'")
    _update_sql(
        connection,
        f"UPDATE {table_name} SET started_at = created_at WHERE status = 'success' AND started_at IS NULL",
    )
    _update_sql(
        connection,
        f"UPDATE {table_name} SET completed_at = updated_at "
        "WHERE status IN ('success', 'failed', 'canceled') AND completed_at IS NULL",
    )


def _backfill_artifact_defaults(connection) -> None:
    table = ImageTaskArtifact.__table__
    if inspect(connection).has_table(table.name):
        _set_null_default(connection, table, "worker_id", "")


def _backfill_schema_defaults(connection) -> None:
    inspector = inspect(connection)
    if inspector.has_table("image_tasks"):
        _backfill_task_defaults(
            connection,
            {str(column["name"]) for column in inspector.get_columns("image_tasks")},
        )
    if inspector.has_table("image_jobs"):
        _backfill_job_defaults(connection)
    if inspector.has_table("image_task_artifacts"):
        _backfill_artifact_defaults(connection)
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        for column in table.columns:
            if column.nullable:
                continue
            default = _migration_default_value(table.name, column.name, column)
            if default is _NO_DEFAULT:
                continue
            _set_null_default(connection, table, column.name, default)


def _repair_schema_shape(connection) -> None:
    if connection.dialect.name == "sqlite":
        # SQLite permits dropping referenced tables while constraints are
        # deferred, but still validates the graph when the transaction ends.
        connection.execute(text("PRAGMA defer_foreign_keys=ON"))
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in table_names:
            table.create(connection, checkfirst=True)
            continue
        actual_columns = {str(column["name"]) for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in actual_columns:
                _add_missing_column(connection, table, column)

    _backfill_schema_defaults(connection)

    inspector = inspect(connection)
    if connection.dialect.name == "sqlite":
        rebuild_names = {
            table.name
            for table in Base.metadata.sorted_tables
            if inspector.has_table(table.name)
            and _table_needs_sqlite_rebuild(inspector, table)
        }
        changed = True
        while changed:
            changed = False
            for table in Base.metadata.sorted_tables:
                if not inspector.has_table(table.name) or table.name in rebuild_names:
                    continue
                references_rebuilt_table = any(
                    str(foreign_key.target_fullname or "").rpartition(".")[0]
                    in rebuild_names
                    for foreign_key in table.foreign_keys
                )
                if references_rebuilt_table:
                    rebuild_names.add(table.name)
                    changed = True
        if rebuild_names:
            _rebuild_sqlite_tables(
                connection,
                [
                    table
                    for table in Base.metadata.sorted_tables
                    if table.name in rebuild_names
                ],
            )
    else:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            actual_columns = {
                str(column["name"]): column
                for column in inspector.get_columns(table.name)
            }
            for column in table.columns:
                actual = actual_columns.get(column.name)
                if actual is None or column.nullable or not bool(actual.get("nullable", True)):
                    continue
                connection.execute(
                    text(
                        f"ALTER TABLE {_table_sql(connection, table)} "
                        f"ALTER COLUMN {_quote_name(connection, column.name)} SET NOT NULL"
                    )
                )
            for signature in _missing_foreign_keys(inspector, table):
                constrained, referred_table, referred = signature
                columns = ", ".join(_quote_name(connection, item) for item in constrained)
                referred_columns = ", ".join(_quote_name(connection, item) for item in referred)
                constraint_name = _quote_name(connection, _foreign_key_name(table, signature))
                ondelete = ""
                for foreign_key in table.foreign_key_constraints:
                    current = (
                        tuple(str(column.parent.name) for column in foreign_key.elements),
                        str(foreign_key.elements[0].target_fullname.rsplit(".", 1)[0]),
                        tuple(str(column.column.name) for column in foreign_key.elements),
                    )
                    if current == signature and foreign_key.elements[0].ondelete:
                        ondelete = f" ON DELETE {foreign_key.elements[0].ondelete}"
                        break
                connection.execute(
                    text(
                        f"ALTER TABLE {_table_sql(connection, table)} "
                        f"ADD CONSTRAINT {constraint_name} FOREIGN KEY ({columns}) "
                        f"REFERENCES {_quote_name(connection, referred_table)} ({referred_columns})"
                        f"{ondelete}"
                    )
                )
            inspector = inspect(connection)

    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in table_names:
            continue
        for index in table.indexes:
            index.create(connection, checkfirst=True)

        actual_unique_sets = _unique_sets(inspector, table.name)
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            columns = _constraint_columns(constraint.columns)
            if columns in actual_unique_sets:
                continue
            name = str(constraint.name or f"uq_{table.name}_{'_'.join(columns)}")
            _create_unique_index(connection, table, name, columns)


def _validate_schema(connection) -> None:
    inspector = inspect(connection)
    errors: list[str] = []
    table_names = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in table_names:
            errors.append(f"{table.name} missing table")
            continue
        actual_column_info = {
            str(column["name"]): column
            for column in inspector.get_columns(table.name)
        }
        actual_columns = set(actual_column_info)
        expected_columns = {str(column.name) for column in table.columns}
        missing_columns = sorted(expected_columns - actual_columns)
        if missing_columns:
            errors.append(f"{table.name} missing columns {', '.join(missing_columns[:8])}")
        for column in table.columns:
            actual = actual_column_info.get(column.name)
            if actual is None:
                continue
            if not _types_compatible(column.type, actual["type"]):
                errors.append(
                    f"{table.name}.{column.name} has incompatible type "
                    f"{actual['type']!s}; expected {column.type!s}"
                )
            if not column.nullable and bool(actual.get("nullable", True)):
                errors.append(f"{table.name}.{column.name} must be NOT NULL")

        unique_sets = _unique_sets(inspector, table.name)
        expected_unique_sets = {
            _constraint_columns(constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected_unique_sets.update({
            _constraint_columns(index.columns)
            for index in table.indexes
            if index.unique
        })
        missing_uniques = sorted(expected_unique_sets - unique_sets)
        if missing_uniques:
            formatted = ", ".join("(" + ", ".join(columns) + ")" for columns in missing_uniques[:4])
            errors.append(f"{table.name} missing unique constraints {formatted}")
        actual_indexes = _index_signatures(inspector, table.name)
        expected_indexes = {
            (_constraint_columns(index.columns), bool(index.unique))
            for index in table.indexes
            if not index.unique
        }
        missing_indexes = sorted(expected_indexes - actual_indexes)
        if missing_indexes:
            formatted = ", ".join(
                "(" + ", ".join(columns) + ")"
                for columns, _unique in missing_indexes[:4]
            )
            errors.append(f"{table.name} missing indexes {formatted}")
        missing_foreign_keys = _missing_foreign_keys(inspector, table)
        if missing_foreign_keys:
            formatted = ", ".join(
                f"({', '.join(constrained)}) -> {referred_table}({', '.join(referred)})"
                for constrained, referred_table, referred in sorted(missing_foreign_keys)
            )
            errors.append(f"{table.name} missing foreign keys {formatted}")
    if errors:
        raise ImageQueueConfigurationError(
            "image queue schema is incomplete: " + "; ".join(errors[:8])
        )


def _migration_v1(connection) -> None:
    _repair_schema_shape(connection)


def _migration_v2(connection) -> None:
    _backfill_schema_defaults(connection)


def _migration_v3(connection) -> None:
    _repair_schema_shape(connection)


def _migration_v4(connection) -> None:
    if inspect(connection).has_table(ImageJob.__table__.name):
        _backfill_job_ordinals(connection)


def _migration_v5(connection) -> None:
    _backfill_schema_defaults(connection)


def _migration_v6(connection) -> None:
    _repair_schema_shape(connection)


def _migration_v7(connection) -> None:
    _repair_schema_shape(connection)


def _migration_v8(connection) -> None:
    _validate_schema(connection)


def _migration_v9(connection) -> None:
    _validate_schema(connection)


def _migration_v10(connection) -> None:
    _repair_schema_shape(connection)


SCHEMA_MIGRATIONS = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
    7: _migration_v7,
    8: _migration_v8,
    9: _migration_v9,
    10: _migration_v10,
}


def _apply_schema_migrations(connection, current_version: int) -> None:
    current_version = int(current_version or 0)
    versions = [
        int(version)
        for (version,) in connection.execute(
            select(ImageQueueSchemaMigration.version).order_by(
                ImageQueueSchemaMigration.version
            )
        ).all()
    ]
    if current_version == 0 and versions:
        current_version = max(versions)
    if current_version > SCHEMA_VERSION:
        raise ImageQueueConfigurationError(
            f"image queue schema version {current_version} is newer than supported {SCHEMA_VERSION}"
        )
    if current_version and versions != list(range(1, current_version + 1)):
        if versions == [SCHEMA_VERSION] and current_version == SCHEMA_VERSION:
            # Older releases wrote only the final marker after shape repair.
            # Validate that shape before reconstructing a complete history.
            _repair_schema_shape(connection)
            _validate_schema(connection)
            for version in range(1, SCHEMA_VERSION):
                connection.execute(
                    ImageQueueSchemaMigration.__table__.insert().values(version=version)
                )
            return
        raise ImageQueueConfigurationError(
            "image queue schema migration history is incomplete"
        )
    for version in range(current_version + 1, SCHEMA_VERSION + 1):
        migration = SCHEMA_MIGRATIONS.get(version)
        if migration is None:
            raise ImageQueueConfigurationError(
                f"image queue migration {version} is not implemented"
            )
        migration(connection)
        connection.execute(
            ImageQueueSchemaMigration.__table__.insert().values(version=version)
        )


class ImageQueueDatabase:
    def __init__(
        self,
        settings: ImageQueueSettings,
        *,
        engine: Engine | None = None,
        allow_non_postgres: bool = False,
    ) -> None:
        self.settings = settings
        self._lock = RLock()
        self._started = False
        self._owns_engine = engine is None
        if engine is None:
            if not settings.database_url:
                self.engine = None
                self._session_factory = None
                return
            if not allow_non_postgres and not settings.database_url.lower().startswith("postgresql"):
                raise ImageQueueConfigurationError("image queue requires PostgreSQL")
            engine_options: dict[str, object] = {"pool_pre_ping": True}
            if settings.database_url.lower().startswith("postgresql"):
                engine_options.update({
                    "pool_size": settings.database_pool_size,
                    "max_overflow": settings.database_max_overflow,
                })
            engine = create_engine(settings.database_url, **engine_options)
        elif not allow_non_postgres and engine.dialect.name != "postgresql":
            raise ImageQueueConfigurationError("image queue requires PostgreSQL")
        self.engine: Engine | None = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @property
    def available(self) -> bool:
        return self.engine is not None

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        if self.engine is None:
            raise ImageQueueUnavailableError("image queue PostgreSQL is not configured")
        with self._lock:
            if self._started:
                return
            with self.engine.begin() as connection:
                postgres = connection.dialect.name == "postgresql"
                if postgres:
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": ADVISORY_LOCK_KEY},
                    )
                try:
                    ensure_database_role_marker(
                        connection,
                        IMAGE_QUEUE_DATABASE_ROLE,
                        create_if_missing=True,
                    )
                except ValueError as exc:
                    raise ImageQueueConfigurationError(str(exc)) from exc
                Base.metadata.create_all(connection)
                current = connection.execute(
                    select(func.max(ImageQueueSchemaMigration.version))
                ).scalar_one_or_none()
                _apply_schema_migrations(connection, int(current or 0))
                _validate_schema(connection)
            self._started = True

    @contextmanager
    def session(self) -> Iterator[Session]:
        if not self._started or self._session_factory is None:
            raise ImageQueueUnavailableError("image queue database is not started")
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except (OperationalError, InterfaceError) as exc:
            session.rollback()
            message = str(exc).lower()
            # SQLite concurrent/static-pool races are not a real outage; re-raise
            # so callers can retry. Only treat connectivity-style failures as 503.
            transient_local = any(
                token in message
                for token in (
                    "cannot start a transaction within a transaction",
                    "no more rows available",
                    "database is locked",
                )
            )
            if transient_local:
                raise
            raise ImageQueueUnavailableError("image queue PostgreSQL is unavailable") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> None:
        with self.session() as session:
            session.execute(text("SELECT 1"))

    def pool_usage_percent(self) -> float:
        if self.engine is None:
            return 100.0
        pool = self.engine.pool
        size = int(getattr(pool, "size", lambda: 0)() or 0)
        overflow = int(getattr(pool, "overflow", lambda: 0)() or 0)
        checked_out = int(getattr(pool, "checkedout", lambda: 0)() or 0)
        capacity = max(1, size + max(0, overflow))
        return min(100.0, checked_out * 100.0 / capacity)

    def dispose(self) -> None:
        with self._lock:
            self._started = False
            if self.engine is not None and self._owns_engine:
                self.engine.dispose()

    def reset_after_fork(self) -> None:
        """Drop pooled connections inherited from the parent process.

        A forked child must never reuse a socket the parent still owns. Passing
        ``close=False`` disposes the pool without closing those connections, so
        the parent keeps working while the child opens its own on next checkout.
        Unlike :meth:`dispose` this keeps the started flag, because the schema is
        already there and the child has to keep issuing queries.
        """
        with self._lock:
            if self.engine is not None and self._owns_engine:
                self.engine.dispose(close=False)
