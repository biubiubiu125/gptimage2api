from __future__ import annotations

from urllib.parse import urlsplit


def normalize_url_origin(url: object) -> tuple[str, str, int] | None:
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = str(parsed.hostname or "").strip().rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def build_public_image_url(base_url: object, relative_path: object) -> str:
    prefix = str(base_url or "").strip().rstrip("/")
    rel = str(relative_path or "").strip().lstrip("/")
    if not rel:
        return prefix
    if not prefix:
        return f"/images/{rel}"
    parsed = urlsplit(prefix)
    if parsed.scheme and parsed.netloc:
        prefix = parsed._replace(query="", fragment="").geturl().rstrip("/")
    path = urlsplit(prefix).path.rstrip("/")
    if path.endswith("/images"):
        return f"{prefix}/{rel}"
    return f"{prefix}/images/{rel}"


def build_public_thumbnail_url(base_url: object, relative_path: object) -> str:
    prefix = str(base_url or "").strip().rstrip("/")
    rel = str(relative_path or "").strip().lstrip("/")
    if not rel:
        return prefix
    if not prefix:
        return f"/image-thumbnails/{rel}"
    parsed = urlsplit(prefix)
    if parsed.scheme and parsed.netloc:
        prefix = parsed._replace(query="", fragment="").geturl().rstrip("/")
    path = urlsplit(prefix).path.rstrip("/")
    if path.endswith("/images"):
        prefix = prefix[: -len("/images")]
    elif path.endswith("/image-thumbnails"):
        return f"{prefix}/{rel}"
    return f"{prefix}/image-thumbnails/{rel}"


def build_public_thumbnail_url_for_image_url(image_url: object, relative_path: object) -> str:
    raw = str(image_url or "").strip().rstrip("/")
    rel = str(relative_path or "").strip().lstrip("/")
    if not raw or not rel:
        return ""
    image_suffix = f"/images/{rel}".rstrip("/")
    thumbnail_suffix = f"/image-thumbnails/{rel}".rstrip("/")

    if raw == image_suffix:
        return thumbnail_suffix
    if raw == image_suffix.lstrip("/"):
        return thumbnail_suffix

    parsed = urlsplit(raw)
    if not (parsed.scheme and parsed.netloc):
        return ""
    clean_url = parsed._replace(query="", fragment="").geturl().rstrip("/")
    path = parsed.path.rstrip("/")
    if path.endswith(thumbnail_suffix):
        return clean_url
    if not path.endswith(image_suffix):
        return ""
    base_path = path[: -len(image_suffix)].rstrip("/")
    base = parsed._replace(path=base_path, query="", fragment="").geturl().rstrip("/")
    return build_public_thumbnail_url(base, rel)


def build_console_image_url(relative_path: object) -> str:
    return build_public_image_url("", relative_path)


def build_console_thumbnail_url(relative_path: object) -> str:
    return build_public_thumbnail_url("", relative_path)


def _owned_image_base_url() -> str:
    try:
        from services.config import config

        return str(config.base_url or "").strip()
    except ValueError:
        return ""


def _is_console_owned_asset_url(parsed) -> bool:
    from services.image_delivery import _is_local_or_private_host

    if _is_local_or_private_host(parsed.hostname or ""):
        return True
    owned = normalize_url_origin(_owned_image_base_url())
    actual = normalize_url_origin(parsed.geturl())
    return owned is not None and actual is not None and owned == actual


def to_same_origin_asset_url(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    path = parsed.path or ""
    if not (path.startswith("/images/") or path.startswith("/image-thumbnails/")):
        return text
    local = path
    if parsed.query:
        local += f"?{parsed.query}"
    if parsed.fragment:
        local += f"#{parsed.fragment}"
    if not parsed.scheme or not parsed.netloc:
        return local
    if _is_console_owned_asset_url(parsed):
        return local
    return text
