"""YAML 读写层：与 Hermes 自身写入器保持同一「策略」，而不是「同一次调用」。

为什么不直接 import ``hermes_cli.config`` / ``utils``：
  * HFK 的定位是独立工具，不能随 Hermes 内部重构而失效；
  * 但写出的 ``config.yaml`` 必须与 Hermes 自己写出的字节形态一致，
    否则每次补丁都会产生整文件级 diff，违反「字段级修改」承诺。

所以本模块**复刻** Hermes v0.23.1 的发射器策略（逐条对照其源码）：
  * ruamel round-trip 模式（保留注释、键序、引号、空行）
  * YAML **1.1** 解析版本（``on/off/yes/no`` 是布尔），并对歧义字符串强制加引号
  * ``width`` 取极大值，禁止长标量折行（Hermes #119844：折行会把 ``D:\\`` 变成空格）
  * 缩进 mapping=2 / sequence=4 / offset=2，``allow_unicode``，不排序键
  * 写入使用「同目录临时文件 + fsync + 原子替换」，并针对 Windows 重命名争用做有界重试

字节级一致性由 tests/test_yaml_parity.py 用 Hermes 自己的写入器做对照验证。
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from hfk.config.keypath import greedy_literal_match, split_key_path

__all__ = [
    "ROUNDTRIP_WIDTH",
    "YAML11_AMBIGUOUS_WORDS",
    "roundtrip_yaml",
    "plain_yaml",
    "read_bytes",
    "read_text",
    "load_document",
    "dump_text",
    "atomic_write_text",
    "update_key",
    "save_document",
    "rt_value",
]

#: 禁止标量折行：ruamel 0.18.x 只有把 width 设到极大才真正关掉折行。
ROUNDTRIP_WIDTH = 2**31 - 1

#: YAML 1.1 下会被解析成布尔的裸字符串，必须加引号。
YAML11_AMBIGUOUS_WORDS = frozenset(
    {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}
)

#: Windows 上「文件被其他句柄占用」导致的 os.replace 失败码。
_CONTENDED_WINERRORS = frozenset({5, 32, 33})
_REPLACE_ATTEMPTS = 6


class _Yaml11Resolver(VersionedResolver):
    """让 ruamel 以 YAML 1.1 语义决定「哪些标量需要引号」。"""

    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 1)


def roundtrip_yaml() -> YAML:
    """新建一个「保留注释/引号」的编辑器实例（不可跨线程共享）。"""
    yaml = YAML(typ="rt")
    yaml.width = ROUNDTRIP_WIDTH
    yaml.Resolver = _Yaml11Resolver
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def plain_yaml() -> YAML:
    """只读校验用：安全模式 + YAML 1.1，与 Hermes 读取侧语义一致。"""
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 1)
    return yaml


def rt_value(value: Any) -> Any:
    """普通 Python 值 → ruamel 节点，歧义字符串（``off``）在任意深度强制双引号。"""
    if isinstance(value, dict):
        node = CommentedMap()
        for key, item in value.items():
            node[key] = rt_value(item)
        return node
    if isinstance(value, (list, tuple)):
        return CommentedSeq(rt_value(item) for item in value)
    if isinstance(value, str) and value.lower() in YAML11_AMBIGUOUS_WORDS:
        return DoubleQuotedScalarString(value)
    return value


# --------------------------------------------------------------------------- 读取


def read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def _decoded(path: Path) -> tuple[str, bool]:
    """按 UTF-8 读取并返回 ``(文本, 是否带 BOM)``。

    关键：**必须做通用换行归一化**（``\\r\\n`` / ``\\r`` → ``\\n``）。
    Hermes 自身用 ``Path.read_text()``，那是 universal-newlines 模式；若这里保留 CRLF，
    ruamel 会把 ``\\r`` 塞进注释 token，回写时再被平台换行翻译一次，
    结果是「每条注释后多一个空行」——实测一份 714 行的 config.yaml 会膨胀到 735 行。
    读入归一化 + 写出走平台换行，才是与 Hermes 完全一致的闭环。
    """
    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig" if had_bom else "utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n"), had_bom


def read_text(path: str | Path) -> str:
    """读取文本（用于「读不出来就绝不写入」的守卫）。"""
    return _decoded(Path(path))[0]


def load_document(path: str | Path) -> tuple[YAML, CommentedMap]:
    """加载为可回写文档；文件缺失/空白时返回空映射。"""
    path = Path(path)
    yaml_rt = roundtrip_yaml()
    if not path.exists():
        return yaml_rt, CommentedMap()
    text = read_text(path)
    data = yaml_rt.load(text) if text.strip() else None
    if data is None:
        return yaml_rt, CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ValueError(
            f"配置顶层必须是映射，实际是 {type(data).__name__}: {path}"
        )
    return yaml_rt, data


def dump_text(document: Any) -> str:
    """把一个文档渲染成字符串（预览 / 差异比对用，不落盘）。"""
    yaml_rt = roundtrip_yaml()
    import io

    buffer = io.StringIO()
    yaml_rt.dump(document, buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- 写入


def _is_contended(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _CONTENDED_WINERRORS


def _publish_path(target: Path) -> Path:
    """符号链接时返回其真实目标，保证写穿链接而不是替换链接。"""
    return Path(os.path.realpath(target)) if target.is_symlink() else target


def _replace_with_retry(tmp: Path, target: Path) -> Path:
    """``os.replace`` 的强化版。

    Windows 上 Hermes 网关可能短暂持有 config.yaml，重命名会以 WinError 32 失败；
    这里做有界退避重试，仍失败则退回「原地重写」（与 Hermes 行为一致：宁可非原子
    也不留下一个写不进去的补丁）。
    """
    real = _publish_path(target)
    delay = 0.02
    last: OSError | None = None
    for _ in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, real)
            return real
        except OSError as exc:
            last = exc
            if not _is_contended(exc):
                raise
            time.sleep(delay)
            delay *= 1.7
    # 争用持续存在：原地重写，保证内容最终落盘。
    payload = tmp.read_bytes()
    with open(real, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        tmp.unlink()
    if last is not None:
        return real
    return real


def _detect_newline(path: Path) -> str:
    """检测文件现有的换行风格，返回 ``"\\r\\n"`` / ``"\\n"`` 或空串（无法判断）。

    为什么必须检测而不能用平台默认：
      HFK 的核心承诺是「恢复后与改动前**字节一致**」。若写入时依赖平台默认换行，
      Windows 上 CRLF 恰好对得上，**Linux/macOS 上就会把 CRLF 变成 LF**，
      字节一致性直接破功（这是 CI 在 ubuntu 上暴露出来的真实缺陷）。
    """
    try:
        raw = path.read_bytes()[:65536]
    except OSError:
        return ""
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    if crlf and crlf >= lone_lf:
        return "\r\n"
    if lone_lf:
        return "\n"
    if crlf:
        return "\r\n"
    return ""


def _apply_newline(text: str, newline: str) -> str:
    """把文本统一成目标换行风格（先归一化再展开，避免出现 ``\\r\\r\\n``）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "\n":
        return normalized
    return normalized.replace("\n", newline)


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    newline: str | None = None,
    add_bom: bool = False,
) -> None:
    """同目录临时文件 + fsync + 原子替换地写入文本。

    ``newline=None``（默认）表示**保留目标文件现有的换行风格**；
    文件不存在时才退回平台默认（Windows CRLF / POSIX LF）。
    这样「改动 → 恢复」在任何平台上都能字节一致。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    effective = newline if newline is not None else (_detect_newline(path) or os.linesep)
    payload = _apply_newline(text, effective)

    mode: int | None = None
    if path.exists():
        with contextlib.suppress(OSError):
            mode = path.stat().st_mode & 0o7777

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        # newline="" 关闭平台转换，换行由 payload 自己决定（上面已按目标风格展开）
        with os.fdopen(fd, "w", encoding="utf-8-sig" if add_bom else "utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replaced = _replace_with_retry(tmp, path)
        if mode is not None:
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(replaced, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """把原始字节原子地写回目标文件（用于失败回滚：字节级还原，不重新序列化）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None = None
    if path.exists():
        with contextlib.suppress(OSError):
            mode = path.stat().st_mode & 0o7777
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replaced = _replace_with_retry(tmp, path)
        if mode is not None:
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(replaced, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _dump_to_file(path: Path, yaml_rt: YAML, document: Any, *, extra_content: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    # 与 atomic_write_text 同一策略：保留目标文件既有换行风格，
    # 否则在非 Windows 平台上会把 CRLF 改写成 LF，破坏字节一致性。
    effective = _detect_newline(path) or os.linesep

    mode: int | None = None
    if path.exists():
        with contextlib.suppress(OSError):
            mode = path.stat().st_mode & 0o7777

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        buffer = io.StringIO()
        yaml_rt.dump(document, buffer)
        if extra_content:
            buffer.write(extra_content)
        payload = _apply_newline(buffer.getvalue(), effective)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replaced = _replace_with_retry(tmp, path)
        if mode is not None:
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(replaced, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def set_in_document(document: Any, key_path: str, value: Any) -> None:
    """在内存文档里设置/删除一个点分键（不落盘）。语义同 :func:`update_key`。"""
    current: Any = document
    keys = split_key_path(key_path)
    i = 0
    while True:
        remaining = keys[i:]
        match = greedy_literal_match(current, remaining)
        segment, consumed = match if match else (remaining[0], 1)
        if i + consumed == len(keys):
            if value is None:
                if isinstance(current, CommentedMap):
                    current.pop(segment, None)
            else:
                current[segment] = value if not isinstance(value, str) else _scalar(value)
            return
        next_value = current.get(segment)
        if not isinstance(next_value, CommentedMap):
            if value is None:
                return  # 父级缺失或是标量：没有可删的键
            next_value = CommentedMap()
            current[segment] = next_value
        current = next_value
        i += consumed


def update_key(path: str | Path, key_path: str, value: Any) -> None:
    """修改**单个**点分键，保留文件其余部分（注释、顺序、引号、Unicode）。

    ``value=None`` 表示删除该键（不是写入 null）。父级缺失时按需创建映射；
    父级是标量/列表时，删除操作直接无效返回（没有可删的键）。
    """
    update_keys(path, {key_path: value})


def update_keys(path: str | Path, updates: Mapping[str, Any]) -> None:
    """在**一次**加载/落盘中修改多个点分键。

    比逐个调用 :func:`update_key` 更安全：只有一次原子替换，不存在「改了一半」的中间状态，
    也正是 Hermes ``write_config_keys`` 的行为。
    """
    path = Path(path)
    yaml_rt, document = load_document(path)
    for key_path, value in updates.items():
        set_in_document(document, key_path, value)
    _dump_to_file(path, yaml_rt, document, extra_content=None)


def _scalar(value: str) -> Any:
    """字符串标量：歧义词强制双引号，其余原样（让 ruamel 自行决定是否需要引号）。"""
    if value.lower() in YAML11_AMBIGUOUS_WORDS:
        return DoubleQuotedScalarString(value)
    return value


def save_document(
    path: str | Path,
    new_state: dict[str, Any],
    *,
    extra_content_on_create: str | None = None,
) -> None:
    """把完整状态合并进现有文档并落盘（``new_state`` 中缺失的键会被删除）。

    与 ``atomic_roundtrip_yaml_save`` 同语义：只重新赋值真正变化的节点，
    未变化的标量/列表保留其行内注释与格式。

    注意：HFK 的补丁流程**不用**这个函数做字段修改（用 :func:`update_key`），
    它只在需要「显式删除」时作为受控入口，且调用前必须已具备完整备份。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    creating = not path.exists() or not read_text(path).strip()
    yaml_rt, existing = load_document(path)

    def _unchanged(current: Any, value: Any) -> bool:
        return current == value and isinstance(current, bool) is isinstance(value, bool)

    def _merge_seq(dst: CommentedSeq, src: list) -> None:
        for index, value in enumerate(src):
            if index < len(dst):
                _merge_item(dst, index, value)
            else:
                dst.append(rt_value(value))
        del dst[len(src) :]

    def _merge_item(dst: Any, key: Any, value: Any) -> None:
        current = dst[key]
        if isinstance(value, dict) and isinstance(current, CommentedMap):
            _merge(current, value)
        elif isinstance(value, list) and isinstance(current, CommentedSeq):
            _merge_seq(current, value)
        elif not _unchanged(current, value):
            dst[key] = rt_value(value)

    def _merge(dst: CommentedMap, src: dict) -> None:
        for key, value in src.items():
            if key in dst:
                _merge_item(dst, key, value)
            else:
                dst[key] = rt_value(value)
        for key in [k for k in dst if k not in src]:
            del dst[key]

    _merge(existing, new_state)
    extra = extra_content_on_create if creating else None
    _dump_to_file(path, yaml_rt, existing, extra_content=extra)
