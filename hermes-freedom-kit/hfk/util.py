"""通用工具：哈希、时间、原子 JSON、路径安全。

刻意保持零第三方依赖，且不导入 Hermes。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "sha256_bytes",
    "sha256_file",
    "utc_now_iso",
    "json_dump_atomic",
    "json_load",
    "ensure_dir",
    "is_within",
    "assert_within",
    "format_bytes",
    "to_plain",
]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    """流式计算文件哈希（配置文件很小，但备份可能较大）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    """ISO-8601 UTC 时间戳（事务记录用，避免本地时区歧义）。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def json_dump_atomic(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """原子写 JSON：先写同目录临时文件，再替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def json_load(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_within(child: str | Path, parent: str | Path) -> bool:
    """``child`` 是否位于 ``parent`` 之内（解析符号链接后比较）。"""
    try:
        child_resolved = Path(child).resolve(strict=False)
        parent_resolved = Path(parent).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if child_resolved == parent_resolved:
        return True
    return parent_resolved in child_resolved.parents


def assert_within(child: str | Path, parent: str | Path, *, what: str = "路径") -> Path:
    """路径越界守卫：不满足则抛 ``PathTraversalError``。"""
    from hfk.errors import PathTraversalError

    if not is_within(child, parent):
        raise PathTraversalError(
            f"{what}越界：{child} 不在允许的根 {parent} 之内"
        )
    return Path(child)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def to_plain(value: Any) -> Any:
    """把 ruamel 的 CommentedMap/CommentedSeq 等递归转成可 JSON 序列化的普通对象。

    事务记录要长期保存、跨进程读取，绝不能依赖 ruamel 的运行时类型。
    """
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
