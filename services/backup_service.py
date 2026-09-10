from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import random
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode
from contextlib import contextmanager
from collections.abc import Callable

from curl_cffi import requests
from sqlalchemy import make_url

from services.application_database import database_backend_name, dispose_all_database_engines
from services.browser_fingerprint import chrome146_headers
from services.config import DATA_DIR, DEFAULT_PROXY_RUNTIME_USER_AGENT, config
from services.http_target import build_http_target_request_options
from services.image_storage_service import IMAGE_INDEX_FILE, WebDAVClient, normalize_image_relative_path
from services.image_tags_service import TAGS_FILE
from services.proxy_service import proxy_settings
from services.storage.coordination_repository import BackupExecutionStateRepository


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    status = _clean(source.get("last_status")) or "idle"
    if status not in {"idle", "running", "success", "error"}:
        status = "idle"
    return {
        "last_started_at": _clean(source.get("last_started_at")) or None,
        "last_finished_at": _clean(source.get("last_finished_at")) or None,
        "last_status": status,
        "last_error": _clean(source.get("last_error")) or None,
        "last_object_key": _clean(source.get("last_object_key")) or None,
    }


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_backup_object(key: object) -> bool:
    name = _clean(key).rsplit("/", 1)[-1]
    return name.startswith("backup-") and (name.endswith(".tar.gz") or name.endswith(".tar.gz.enc"))


def _backup_key_in_scope(key: object, settings: dict[str, object]) -> bool:
    raw = _clean(key)
    candidate = raw.replace("\\", "/")
    prefix = _clean(settings.get("prefix")).strip("/") or "backups"
    prefix_with_separator = f"{prefix}/"
    if (
        not candidate
        or candidate != raw
        or not candidate.startswith(prefix_with_separator)
        or any(part in {"", ".", ".."} for part in candidate.split("/"))
    ):
        return False
    name = candidate[len(prefix_with_separator):]
    return "/" not in name and _is_backup_object(candidate)


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _openssl_encrypt(data: bytes, passphrase: str) -> bytes:
    env = dict(os.environ)
    env["GPTIMAGE2API_BACKUP_PASSPHRASE"] = passphrase
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-md",
                "sha256",
                "-pass",
                "env:GPTIMAGE2API_BACKUP_PASSPHRASE",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError("当前环境缺少 openssl，无法执行加密备份") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise BackupError(f"加密备份失败：{detail or 'openssl 执行失败'}") from exc
    return result.stdout


def _openssl_decrypt(data: bytes, passphrase: str) -> bytes:
    env = dict(os.environ)
    env["GPTIMAGE2API_BACKUP_PASSPHRASE"] = passphrase
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-md",
                "sha256",
                "-pass",
                "env:GPTIMAGE2API_BACKUP_PASSPHRASE",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError("当前环境缺少 openssl，无法解密备份") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise BackupError(f"备份解密失败：{detail or 'openssl 执行失败'}") from exc
    if not result.stdout:
        raise BackupError("备份解密失败：openssl 未输出数据")
    return result.stdout


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class BackupError(RuntimeError):
    pass


class CloudflareR2Client:
    def __init__(self, settings: dict[str, object]) -> None:
        self.account_id = _clean(settings.get("account_id"))
        self.access_key_id = _clean(settings.get("access_key_id"))
        self.secret_access_key = _clean(settings.get("secret_access_key"))
        self.bucket = _clean(settings.get("bucket"))
        self.prefix = _clean(settings.get("prefix")) or "backups"
        self.session = requests.Session(**proxy_settings.build_session_kwargs(
            resource=True,
            upstream=True,
            verify=not proxy_settings.should_skip_ssl_verify(),
        ))
        self.session.headers.update(chrome146_headers({"User-Agent": DEFAULT_PROXY_RUNTIME_USER_AGENT}))

    def validate(self) -> None:
        missing = []
        if not self.account_id:
            missing.append("Account ID")
        if not self.access_key_id:
            missing.append("Access Key ID")
        if not self.secret_access_key:
            missing.append("Secret Access Key")
        if not self.bucket:
            missing.append("Bucket")
        if missing:
            raise BackupError(f"R2 配置不完整：缺少 {'、'.join(missing)}")

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def _aws_v4_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        now = _utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        encoded_query = urlencode(sorted((query or {}).items()))
        payload_hash = _sha256_hex(body)
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            for key, value in extra_headers.items():
                headers[key.lower()] = value.strip()
        sorted_items = sorted((key.lower(), " ".join(str(value).strip().split())) for key, value in headers.items())
        canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted_items)
        signed_headers = ";".join(key for key, _ in sorted_items)
        canonical_request = "\n".join([
            method.upper(),
            path,
            encoded_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        k_date = _hmac_sha256(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        k_region = hmac.new(k_date, b"auto", hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        request_headers = {key: value for key, value in headers.items()}
        request_headers["authorization"] = authorization
        return encoded_query, request_headers

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ):
        object_path = f"/{self.bucket}"
        if key:
            object_path += f"/{quote(key.lstrip('/'), safe='/')}"
        encoded_query, headers = self._aws_v4_headers(method, object_path, query=query, body=body, extra_headers=extra_headers)
        url = f"{self.endpoint}{object_path}"
        if encoded_query:
            url += f"?{encoded_query}"
        response = self.session.request(
            method.upper(),
            url,
            headers=headers,
            data=body,
            timeout=timeout,
            **build_http_target_request_options(url),
        )
        return response

    def test_connection(self) -> dict[str, object]:
        self.validate()
        response = self._request("GET", query={"list-type": "2", "max-keys": "1"}, timeout=30.0)
        if response.status_code >= 400:
            raise BackupError(f"连接 R2 失败：HTTP {response.status_code}")
        return {"ok": True, "status": int(response.status_code)}

    def upload_bytes(self, key: str, payload: bytes, *, content_type: str, metadata: dict[str, str] | None = None) -> dict[str, object]:
        headers = {"content-type": content_type}
        if metadata:
            for item_key, item_value in metadata.items():
                headers[f"x-amz-meta-{item_key}"] = str(item_value)
        response = self._request("PUT", key, body=payload, extra_headers=headers)
        if response.status_code >= 400:
            raise BackupError(f"上传备份失败：HTTP {response.status_code}")
        return {"key": key, "etag": str(response.headers.get("etag") or "").strip('"')}

    def download_bytes(self, key: str) -> bytes:
        response = self._request("GET", key, timeout=120.0)
        if response.status_code >= 400:
            raise BackupError(f"下载备份失败：HTTP {response.status_code}")
        payload = bytes(response.content or b"")
        if not payload:
            raise BackupError("下载备份失败：对象内容为空")
        return payload

    def delete_object(self, key: str) -> None:
        response = self._request("DELETE", key, timeout=30.0)
        if response.status_code >= 400 and response.status_code != 404:
            raise BackupError(f"删除备份失败：HTTP {response.status_code}")

    def list_objects(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        continuation = ""
        while True:
            query = {"list-type": "2", "prefix": f"{self.prefix.rstrip('/')}/", "max-keys": "1000"}
            if continuation:
                query["continuation-token"] = continuation
            response = self._request("GET", query=query, timeout=30.0)
            if response.status_code >= 400:
                raise BackupError(f"获取备份列表失败：HTTP {response.status_code}")
            text = response.text
            for block in text.split("<Contents>")[1:]:
                key = _clean(block.split("<Key>", 1)[1].split("</Key>", 1)[0]) if "<Key>" in block else ""
                if not key:
                    continue
                size_text = _clean(block.split("<Size>", 1)[1].split("</Size>", 1)[0]) if "<Size>" in block else "0"
                updated = _clean(block.split("<LastModified>", 1)[1].split("</LastModified>", 1)[0]) if "<LastModified>" in block else ""
                items.append({
                    "key": key,
                    "size": int(size_text or 0),
                    "updated_at": updated,
                })
            truncated = "<IsTruncated>true</IsTruncated>" in text
            if not truncated:
                break
            if "<NextContinuationToken>" not in text:
                break
            continuation = _clean(text.split("<NextContinuationToken>", 1)[1].split("</NextContinuationToken>", 1)[0])
            if not continuation:
                break
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def close(self) -> None:
        self.session.close()


class BackupService:
    def __init__(
        self,
        *,
        repository: BackupExecutionStateRepository | None = None,
        database_url: str | None = None,
    ) -> None:
        if repository is not None and database_url is not None:
            raise ValueError("provide repository or database_url, not both")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._restore_lock = threading.RLock()
        self._restore_active = False
        self._restore_before_hook: Callable[[], None] | None = None
        self._restore_after_hook: Callable[[], None] | None = None
        self._repository = repository or BackupExecutionStateRepository(database_url)
        self._recover_interrupted_execution()

    def configure_restore_hooks(
        self,
        before: Callable[[], None] | None = None,
        after: Callable[[], None] | None = None,
    ) -> None:
        service_lock = getattr(self, "_lock", None)
        if service_lock is None:
            service_lock = threading.RLock()
            self._lock = service_lock
        with service_lock:
            self._restore_before_hook = before
            self._restore_after_hook = after

    @contextmanager
    def _restore_scope(self):
        restore_lock = getattr(self, "_restore_lock", None)
        if restore_lock is None:
            restore_lock = threading.RLock()
            self._restore_lock = restore_lock
        with restore_lock:
            service_lock = getattr(self, "_lock", None)
            if service_lock is None:
                service_lock = threading.RLock()
                self._lock = service_lock
            with service_lock:
                if bool(getattr(self, "_restore_active", False)):
                    raise BackupError("当前已有恢复任务正在执行")
                self._restore_active = True
                before = getattr(self, "_restore_before_hook", None)
                after = getattr(self, "_restore_after_hook", None)
            try:
                if callable(before):
                    before()
                yield
            except BackupError:
                raise
            except Exception as exc:
                raise BackupError(f"恢复维护准备失败：{exc}") from exc
            finally:
                try:
                    if callable(after):
                        after()
                finally:
                    with service_lock:
                        self._restore_active = False

    def _recover_interrupted_execution(self) -> None:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                state = _normalize_backup_state(self._repository.load())
                if state["last_status"] != "running":
                    return
                state.update({
                    "last_finished_at": _iso_now(),
                    "last_status": "error",
                    "last_error": "备份执行被进程重启中断",
                })
                self._repository.replace(state)
        except TimeoutError:
            # Another process still owns the backup execution lease.
            return

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="r2-backup-scheduler")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_scheduled_backup_if_needed()
            except Exception:
                pass
            self._stop_event.wait(30)

    def run_scheduled_backup_if_needed(self) -> None:
        settings = config.get_backup_settings()
        if not settings.get("enabled"):
            return
        state = self.get_status()
        if state.get("running"):
            return
        interval_minutes = int(settings.get("interval_minutes") or 360)
        last_finished_raw = _clean(state.get("last_finished_at"))
        if last_finished_raw:
            try:
                last_finished = datetime.fromisoformat(last_finished_raw.replace("Z", "+00:00"))
                elapsed = (_utc_now() - last_finished.astimezone(UTC)).total_seconds()
                if elapsed < interval_minutes * 60:
                    return
            except Exception:
                pass
        self.run_backup(trigger="schedule")

    def get_status(self) -> dict[str, object]:
        state = _normalize_backup_state(self._repository.load())
        return {
            **state,
            "running": self._running or state["last_status"] == "running",
            "restoring": bool(getattr(self, "_restore_active", False)),
        }

    def is_configured(self) -> bool:
        settings = config.get_backup_settings()
        return all([
            _clean(settings.get("account_id")),
            _clean(settings.get("access_key_id")),
            _clean(settings.get("secret_access_key")),
            _clean(settings.get("bucket")),
        ])

    def get_settings(self) -> dict[str, object]:
        settings = dict(config.get_backup_settings())
        settings["secret_access_key"] = "********" if _clean(settings.get("secret_access_key")) else ""
        settings["passphrase"] = "********" if _clean(settings.get("passphrase")) else ""
        return settings

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        current = config.get_backup_settings()
        merged = dict(current)
        merged.update(dict(payload or {}))
        if "include" in payload and isinstance(payload.get("include"), dict):
            include = dict(current.get("include") or {})
            include.update(payload.get("include") or {})
            merged["include"] = include
        if payload.get("secret_access_key") == "********":
            merged["secret_access_key"] = current.get("secret_access_key")
        if payload.get("passphrase") == "********":
            merged["passphrase"] = current.get("passphrase")
        updated = config.update({"backup": merged})
        return dict(updated.get("backup") or {})

    def test_connection(self) -> dict[str, object]:
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            return client.test_connection()
        finally:
            client.close()

    def list_backups(self) -> list[dict[str, object]]:
        if not self.is_configured():
            return []
        settings = config.get_backup_settings()
        client = CloudflareR2Client(settings)
        try:
            items = client.list_objects()
        finally:
            client.close()
        parsed: list[dict[str, object]] = []
        for item in items:
            key = _clean(item.get("key"))
            if not _backup_key_in_scope(key, settings):
                continue
            name = key.rsplit("/", 1)[-1]
            encrypted = name.endswith(".enc")
            parsed.append({
                "key": key,
                "name": name,
                "size": int(item.get("size") or 0),
                "updated_at": item.get("updated_at"),
                "encrypted": encrypted,
            })
        return parsed

    def delete_backup(self, key: str) -> None:
        settings = config.get_backup_settings()
        candidate = _clean(key)
        if not _backup_key_in_scope(candidate, settings):
            raise BackupError("备份对象 key 无效")
        client = CloudflareR2Client(settings)
        try:
            client.delete_object(candidate)
        finally:
            client.close()

    def restore_backup(self, key: str) -> dict[str, object]:
        with self._restore_scope():
            settings = config.get_backup_settings()
            candidate = _clean(key)
            if not _backup_key_in_scope(candidate, settings):
                raise BackupError("备份对象 key 无效")
            with self._repository.run_lock(timeout_seconds=0):
                with self._lock:
                    if self._running:
                        raise BackupError("当前已有备份任务正在执行，无法恢复")
                client = CloudflareR2Client(settings)
                try:
                    client.validate()
                    payload = client.download_bytes(candidate)
                finally:
                    client.close()
            if candidate.endswith(".enc"):
                passphrase = _clean(settings.get("passphrase"))
                if not passphrase:
                    raise BackupError("加密备份未配置解密口令")
                payload = _openssl_decrypt(payload, passphrase)
            return self._restore_archive_bytes(payload)

    def restore_archive_bytes(self, payload: bytes) -> dict[str, object]:
        with self._restore_scope():
            return self._restore_archive_bytes(payload)

    def _restore_archive_bytes(self, payload: bytes) -> dict[str, object]:
        if not payload:
            raise BackupError("备份内容为空")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".gptimage2api-restore-",
            dir=DATA_DIR,
        ) as temporary_dir:
            staging = Path(temporary_dir)
            metadata = self._extract_backup_archive(payload, staging)
            try:
                version = int(metadata.get("version") or 0)
            except (TypeError, ValueError) as exc:
                raise BackupError("备份 metadata.version 无效") from exc
            if version < 4:
                raise BackupError("备份版本过旧，至少需要版本 4")

            database_backend = _clean(metadata.get("database_backend")).lower()
            current_backend = database_backend_name(self._repository.database_url)
            if database_backend != current_backend:
                raise BackupError(
                    f"备份数据库类型 {database_backend or 'unknown'} "
                    f"与当前 Application Database 类型 {current_backend} 不一致"
                )
            database_name = (
                "data/application-database.sqlite3"
                if database_backend == "sqlite"
                else "data/application-database.pgdump"
            )
            database_source = staging / database_name
            if not database_source.is_file():
                raise BackupError(f"备份缺少 Application Database：{database_name}")

            restored: dict[str, bool] = {
                "application_database": False,
                "image_tasks": False,
                "editable_files": False,
                "images": False,
                "image_queue": False,
                "registration": False,
            }
            if database_backend == "sqlite":
                self._restore_sqlite_database(database_source, self._repository.database_url)
            elif database_backend == "postgresql":
                self._restore_postgresql_database(database_source, self._repository.database_url, "Application Database")
            else:
                raise BackupError(f"不支持恢复数据库类型：{database_backend or 'unknown'}")
            restored["application_database"] = True

            image_tasks_source = staging / "data/image_tasks.json"
            image_index_source = staging / "data/image_index.json"
            if image_tasks_source.is_file():
                self._restore_file(image_tasks_source, DATA_DIR / "image_tasks.json")
                restored["image_tasks"] = True
            if image_index_source.is_file():
                self._restore_file(image_index_source, IMAGE_INDEX_FILE)
                restored["image_tasks"] = True

            editable_source = staging / "data/files"
            if editable_source.is_dir():
                self._restore_directory(editable_source, DATA_DIR / "files")
                restored["editable_files"] = True

            tags_source = staging / "data/image_tags.json"
            images_source = staging / "data/images"
            if tags_source.is_file():
                self._restore_file(tags_source, TAGS_FILE)
                restored["images"] = True
            if images_source.is_dir():
                self._restore_directory(images_source, config.images_dir)
                restored["images"] = True

            queue_settings = metadata.get("image_queue_database")
            if isinstance(queue_settings, dict) and bool(queue_settings.get("included")):
                queue_source = staging / "data/image-queue.pgdump"
                if not queue_source.is_file():
                    raise BackupError("备份 metadata 声明包含 Image Queue Store，但缺少队列快照")
                queue_database_url = self._image_queue_database_url()
                self._restore_postgresql_database(
                    queue_source,
                    queue_database_url,
                    "Image Queue Store",
                )
                restored["image_queue"] = True

            registration_settings = metadata.get("registration_state")
            if isinstance(registration_settings, dict) and bool(registration_settings.get("included")):
                allowed_files = {
                    "outlook_token_used.json",
                    "register_core_results_pending.json",
                    "remail_dead_mailboxes.json",
                }
                declared_files = registration_settings.get("files")
                if isinstance(declared_files, list):
                    unknown = {
                        _clean(item)
                        for item in declared_files
                        if _clean(item) and _clean(item) not in {
                            f"data/{name}" for name in allowed_files
                        }
                    }
                    if unknown:
                        raise BackupError("备份 registration_state.files 包含不受支持的路径")
                for filename in sorted(allowed_files):
                    source = staging / "data" / filename
                    if source.is_file():
                        self._restore_file(source, DATA_DIR / filename)
                        restored["registration"] = True

            return {
                "ok": True,
                "metadata": {
                    "version": version,
                    "created_at": _clean(metadata.get("created_at")),
                    "trigger": _clean(metadata.get("trigger")),
                },
                "restored": restored,
                "requires_restart": True,
            }

    @staticmethod
    def _safe_restore_target(staging: Path, name: str) -> Path:
        normalized = str(name or "").replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if (
            not normalized
            or normalized.startswith("/")
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise BackupError(f"备份包含不安全的路径：{name}")
        root = staging.resolve()
        target = (root.joinpath(*parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise BackupError(f"备份包含越界路径：{name}") from exc
        return target

    def _extract_backup_archive(self, payload: bytes, staging: Path) -> dict[str, object]:
        total_size = 0
        seen: set[str] = set()
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
        except (OSError, tarfile.TarError) as exc:
            raise BackupError("备份不是有效的 tar.gz 文件") from exc
        with archive:
            for member in archive.getmembers():
                normalized_name = str(member.name or "").replace("\\", "/")
                if normalized_name in seen:
                    raise BackupError(f"备份包含重复路径：{normalized_name}")
                seen.add(normalized_name)
                target = self._safe_restore_target(staging, normalized_name)
                if member.issym() or member.islnk() or member.isdev():
                    raise BackupError(f"备份不允许包含链接或设备文件：{normalized_name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise BackupError(f"备份包含不支持的文件类型：{normalized_name}")
                if member.size < 0 or member.size > 4 * 1024 * 1024 * 1024:
                    raise BackupError(f"备份文件过大：{normalized_name}")
                total_size += member.size
                if total_size > 8 * 1024 * 1024 * 1024:
                    raise BackupError("备份展开总大小超过限制")
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError(f"无法读取备份文件：{normalized_name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

        metadata_path = staging / "backup-metadata.json"
        if not metadata_path.is_file():
            raise BackupError("备份缺少 backup-metadata.json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError("备份 metadata 无法解析") from exc
        if not isinstance(metadata, dict):
            raise BackupError("备份 metadata 必须是对象")
        return metadata

    @staticmethod
    def _restore_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".restore",
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as source_file:
                    shutil.copyfileobj(source_file, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _restore_directory(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _restore_sqlite_database(source: Path, database_url: str) -> None:
        url = make_url(database_url)
        database_path = _clean(url.database)
        if not database_path or database_path == ":memory:":
            raise BackupError("SQLite Application Database 必须是文件数据库")
        target = Path(database_path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        dispose_all_database_engines()
        BackupService._restore_file(source, target)
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)

    @staticmethod
    def _restore_postgresql_database(source: Path, database_url: str, label: str) -> None:
        url = make_url(database_url)
        command = [
            "pg_restore",
            "--exit-on-error",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
        ]
        if url.host:
            command.extend(["--host", url.host])
        if url.port:
            command.extend(["--port", str(url.port)])
        if url.username:
            command.extend(["--username", url.username])
        if url.database:
            command.extend(["--dbname", url.database])
        command.append(str(source))
        env = dict(os.environ)
        if url.password:
            env["PGPASSWORD"] = url.password
        sslmode = str(url.query.get("sslmode") or "").strip()
        if sslmode:
            env["PGSSLMODE"] = sslmode
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise BackupError(f"当前环境缺少 pg_restore，无法恢复 {label}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise BackupError(f"{label} 恢复失败：{detail or 'pg_restore 执行失败'}") from exc

    def run_backup(self, *, trigger: str = "manual") -> dict[str, object]:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                with self._lock:
                    current = self.get_status()
                    if bool(getattr(self, "_restore_active", False)):
                        raise BackupError("当前正在恢复备份，无法启动新的备份任务")
                    if self._running:
                        raise BackupError("当前已有备份任务正在执行")
                    started_at = _iso_now()
                    self._running = True
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": current.get("last_finished_at"),
                        "last_status": "running",
                        "last_error": None,
                        "last_object_key": current.get("last_object_key"),
                    })
                try:
                    result = self._run_backup_once(trigger=trigger)
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "success",
                        "last_error": None,
                        "last_object_key": result["key"],
                    })
                    return result
                except Exception as exc:
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "error",
                        "last_error": str(exc) or exc.__class__.__name__,
                        "last_object_key": current.get("last_object_key"),
                    })
                    raise
                finally:
                    self._running = False
        except TimeoutError as exc:
            raise BackupError("当前已有备份任务正在执行") from exc

    def _run_backup_once(self, *, trigger: str) -> dict[str, object]:
        settings = config.get_backup_settings()
        self._validate_backup_security(settings)
        client = CloudflareR2Client(settings)
        client.validate()
        payload_raw = self._build_backup_archive(settings, trigger=trigger)
        encrypted = bool(settings.get("encrypt"))
        if encrypted:
            passphrase = _clean(settings.get("passphrase"))
            if not passphrase:
                raise BackupError("已启用备份加密，但未设置加密口令")
            payload = _openssl_encrypt(payload_raw, passphrase)
            suffix = ".tar.gz.enc"
        else:
            payload = payload_raw
            suffix = ".tar.gz"
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        random_tag = f"{random.randint(0, 0xFFFF):04x}"
        object_key = f"{client.prefix.rstrip('/')}/backup-{timestamp}-{random_tag}{suffix}"
        metadata = {
            "created-at": _iso_now(),
            "encrypted": "true" if encrypted else "false",
            "trigger": trigger,
        }
        try:
            result = client.upload_bytes(object_key, payload, content_type="application/octet-stream", metadata=metadata)
            self._apply_rotation(client, int(settings.get("rotation_keep") or 0))
            return {
                "key": result["key"],
                "size": len(payload),
                "encrypted": encrypted,
            }
        finally:
            client.close()

    @staticmethod
    def _validate_backup_security(settings: dict[str, object]) -> None:
        if not bool(settings.get("encrypt")):
            raise BackupError(
                "备份包含 Application Database、Image Queue Store 与注册状态等敏感数据，"
                "必须启用备份加密"
            )
        if not _clean(settings.get("passphrase")):
            raise BackupError("已启用备份加密，但未设置加密口令")

    def _apply_rotation(self, client: CloudflareR2Client, keep: int) -> None:
        if keep <= 0:
            return
        items = [item for item in client.list_objects() if _is_backup_object(item.get("key"))]
        if len(items) <= keep:
            return
        for item in items[keep:]:
            key = _clean(item.get("key"))
            if key:
                client.delete_object(key)

    def _build_backup_archive(self, settings: dict[str, object], *, trigger: str) -> bytes:
        include = settings.get("include") if isinstance(settings.get("include"), dict) else {}
        database_backend = database_backend_name(self._repository.database_url)
        include_image_queue = bool(include.get("image_queue"))
        image_queue_database_url = (
            self._image_queue_database_url()
            if include_image_queue
            else ""
        )
        registration_state_files = [
            "data/outlook_token_used.json",
            "data/register_core_results_pending.json",
            "data/remail_dead_mailboxes.json",
        ]
        metadata = {
            "version": 5,
            "created_at": _iso_now(),
            "trigger": trigger,
            "app_version": config.app_version,
            "database_backend": database_backend,
            "application_database": config.get_storage_backend().get_backend_info(),
            "image_queue_database": {
                "included": include_image_queue,
                "configured": bool(image_queue_database_url),
                "database_url": self._masked_database_url(image_queue_database_url),
            },
            "registration_state": {
                "included": bool(include.get("register")),
                "files": registration_state_files if include.get("register") else [],
            },
        }
        contents = {
            "present": [],
            "generated_defaults": [],
            "omitted": [],
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            database_name = (
                "data/application-database.sqlite3"
                if database_backend == "sqlite"
                else "data/application-database.pgdump"
            )
            self._add_bytes_to_archive(
                archive,
                database_name,
                self._application_database_backup(database_backend),
            )
            contents["present"].append(database_name)
            if include.get("image_tasks"):
                self._add_file_to_archive(
                    archive,
                    DATA_DIR / "image_tasks.json",
                    "data/image_tasks.json",
                    default_payload=b"[]\n",
                    contents=contents,
                )
                self._add_file_to_archive(
                    archive,
                    IMAGE_INDEX_FILE,
                    "data/image_index.json",
                    default_payload=b'{"items": {}}\n',
                    contents=contents,
                )
            if include.get("editable_files"):
                self._add_directory_to_archive(
                    archive,
                    DATA_DIR / "files",
                    "data/files",
                    contents=contents,
                )
            if include.get("images"):
                self._add_file_to_archive(
                    archive,
                    TAGS_FILE,
                    "data/image_tags.json",
                    default_payload=b"{}\n",
                    contents=contents,
                )
                self._add_directory_to_archive(
                    archive,
                    config.images_dir,
                    "data/images",
                    contents=contents,
                )
                self._add_remote_only_image_assets_to_archive(archive, contents)
            if include_image_queue:
                self._add_bytes_to_archive(
                    archive,
                    "data/image-queue.pgdump",
                    self._postgresql_database_backup(image_queue_database_url),
                )
                contents["present"].append("data/image-queue.pgdump")
            if include.get("register"):
                self._add_registration_state_files(archive, contents=contents)
            if contents["omitted"]:
                raise BackupError(
                    "备份包含未能读取的文件："
                    + "、".join(contents["omitted"])
                )
            metadata["contents"] = contents
            self._add_bytes_to_archive(archive, "backup-metadata.json", _json_bytes(metadata))
        return buffer.getvalue()

    @staticmethod
    def _masked_database_url(database_url: str) -> str:
        if not database_url:
            return ""
        try:
            return make_url(database_url).render_as_string(hide_password=True)
        except Exception:
            return "invalid-database-url"

    @staticmethod
    def _image_queue_database_url() -> str:
        from services.image_queue.settings import (
            ImageQueueConfigurationError,
            ImageQueueSettings,
        )

        try:
            settings = ImageQueueSettings.from_env()
        except ImageQueueConfigurationError as exc:
            raise BackupError(f"Image Queue Store 配置无效：{exc}") from exc
        if not settings.database_url:
            raise BackupError(
                "Image Queue Store 未配置 PostgreSQL，无法创建完整备份"
            )
        return settings.database_url

    def _add_registration_state_files(
        self,
        archive: tarfile.TarFile,
        *,
        contents: dict[str, list[str]],
    ) -> None:
        defaults = {
            "outlook_token_used.json": b"{}\n",
            "register_core_results_pending.json": b"[]\n",
            "remail_dead_mailboxes.json": b"[]\n",
        }
        for filename in (
            "outlook_token_used.json",
            "register_core_results_pending.json",
            "remail_dead_mailboxes.json",
        ):
            self._add_file_to_archive(
                archive,
                DATA_DIR / filename,
                f"data/{filename}",
                default_payload=defaults[filename],
                contents=contents,
            )

    def _application_database_backup(self, backend: str) -> bytes:
        if backend == "sqlite":
            return self._sqlite_database_backup()
        if backend == "postgresql":
            return self._postgresql_database_backup()
        raise BackupError(f"不支持备份数据库类型：{backend}")

    def _sqlite_database_backup(self) -> bytes:
        temporary_path = ""
        raw_connection = self._repository.engine.raw_connection()
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as temporary:
                temporary_path = temporary.name
            destination = sqlite3.connect(temporary_path)
            try:
                raw_connection.driver_connection.backup(destination)
            finally:
                destination.close()
            return Path(temporary_path).read_bytes()
        except Exception as exc:
            raise BackupError("创建 SQLite Application Database 快照失败") from exc
        finally:
            raw_connection.close()
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def _postgresql_database_backup(self, database_url: str | None = None) -> bytes:
        url = make_url(database_url or self._repository.database_url)
        command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
        if url.host:
            command.extend(["--host", url.host])
        if url.port:
            command.extend(["--port", str(url.port)])
        if url.username:
            command.extend(["--username", url.username])
        if url.database:
            command.extend(["--dbname", url.database])

        env = dict(os.environ)
        if url.password:
            env["PGPASSWORD"] = url.password
        sslmode = str(url.query.get("sslmode") or "").strip()
        if sslmode:
            env["PGSSLMODE"] = sslmode
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise BackupError("当前环境缺少 pg_dump，无法备份 PostgreSQL") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise BackupError(f"PostgreSQL 备份失败：{detail or 'pg_dump 执行失败'}") from exc
        if not result.stdout:
            raise BackupError("PostgreSQL 备份失败：pg_dump 未输出数据")
        return result.stdout

    def _add_bytes_to_archive(self, archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = int(_utc_now().timestamp())
        archive.addfile(info, io.BytesIO(payload))

    def _add_file_to_archive(
        self,
        archive: tarfile.TarFile,
        source: Path,
        arcname: str,
        *,
        default_payload: bytes | None = None,
        contents: dict[str, list[str]] | None = None,
    ) -> None:
        if not source.exists() or not source.is_file():
            if default_payload is None:
                if contents is not None:
                    contents["omitted"].append(arcname)
                return
            self._add_bytes_to_archive(archive, arcname, default_payload)
            if contents is not None:
                contents["generated_defaults"].append(arcname)
            return
        archive.add(source, arcname=arcname)
        if contents is not None:
            contents["present"].append(arcname)

    def _add_directory_to_archive(
        self,
        archive: tarfile.TarFile,
        source_dir: Path,
        arcname_root: str,
        *,
        contents: dict[str, list[str]] | None = None,
    ) -> None:
        directory_name = arcname_root.rstrip("/") + "/"
        if not source_dir.exists() or not source_dir.is_dir():
            info = tarfile.TarInfo(name=directory_name)
            info.type = tarfile.DIRTYPE
            info.mtime = int(_utc_now().timestamp())
            archive.addfile(info)
            if contents is not None:
                contents["generated_defaults"].append(arcname_root)
            return
        info = tarfile.TarInfo(name=directory_name)
        info.type = tarfile.DIRTYPE
        info.mtime = int(_utc_now().timestamp())
        archive.addfile(info)
        if contents is not None:
            contents["present"].append(arcname_root)
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source_dir).as_posix()
                archive.add(path, arcname=f"{arcname_root}/{relative}")
                if contents is not None:
                    contents["present"].append(f"{arcname_root}/{relative}")

    def _add_remote_only_image_assets_to_archive(
        self,
        archive: tarfile.TarFile,
        contents: dict[str, list[str]],
    ) -> None:
        if not IMAGE_INDEX_FILE.is_file():
            return
        try:
            index = json.loads(IMAGE_INDEX_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError("读取图片索引失败，无法备份 WebDAV 图片") from exc
        if not isinstance(index, dict):
            raise BackupError("图片索引格式无效，无法备份 WebDAV 图片")
        items = index.get("items")
        if items is None:
            return
        if not isinstance(items, dict):
            raise BackupError("图片索引 items 格式无效，无法备份 WebDAV 图片")

        images_root = Path(config.images_dir).resolve()
        remote_only: list[str] = []
        seen: set[str] = set()
        for raw_rel, raw_item in items.items():
            if not isinstance(raw_rel, str) or not isinstance(raw_item, dict):
                raise BackupError("图片索引包含无效条目，无法备份 WebDAV 图片")
            if not raw_item.get("webdav") or raw_item.get("local"):
                continue
            try:
                safe_rel = normalize_image_relative_path(raw_rel)
            except Exception as exc:
                raise BackupError(f"图片索引路径无效：{raw_rel}") from exc
            if Path(safe_rel).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            local_path = (images_root / safe_rel).resolve()
            try:
                local_path.relative_to(images_root)
            except ValueError as exc:
                raise BackupError(f"图片索引路径越界：{raw_rel}") from exc
            if local_path.is_file() or safe_rel in seen:
                continue
            seen.add(safe_rel)
            remote_only.append(safe_rel)

        if not remote_only:
            return
        get_settings = getattr(config, "get_image_storage_settings", None)
        if not callable(get_settings):
            raise BackupError("图片索引包含 WebDAV-only 图片，但 WebDAV 配置不可用")
        settings = get_settings()
        if not isinstance(settings, dict) or (
            str(settings.get("mode") or "").strip().lower() not in {"webdav", "both"}
            or not str(settings.get("webdav_url") or "").strip()
        ):
            raise BackupError("图片索引包含 WebDAV-only 图片，但 WebDAV 配置不可用")

        try:
            with WebDAVClient(settings) as client:
                for safe_rel in remote_only:
                    try:
                        payload = client.get(safe_rel)
                    except Exception as exc:
                        raise BackupError(f"备份 WebDAV 图片失败：{safe_rel}") from exc
                    if not payload:
                        raise BackupError(f"备份 WebDAV 图片失败：{safe_rel} 内容为空")
                    arcname = f"data/images/{safe_rel}"
                    self._add_bytes_to_archive(archive, arcname, bytes(payload))
                    contents["present"].append(arcname)
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("备份 WebDAV 图片失败") from exc


backup_service = BackupService()
