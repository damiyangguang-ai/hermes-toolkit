"""配置键路径（dotted key path）解析与导航。

必须与 Hermes 自身 ``hermes config set`` / ``save_config_value`` 的语义完全一致，
否则 HFK 写入的键会被 Hermes 读成另一个位置（历史事故：``glm-5.3`` 被拆成
``glm-5: {3: ...}`` 幻影兄弟节点）。

三条规则（均来自 Hermes v0.23.1 实现）：
1. ``.`` 是层级分隔符，``\\.`` 表示键名中的字面点。
2. 在**已存在**的映射里导航时，优先匹配「最长字面点键」（``grok-4.6`` 整体作为一个键）。
3. 写入若要新建中间映射，而同级已存在以 ``<part>.`` 开头的字面键，则会产生
   运行时永远读不到的幻影节点——此处提供检测函数供校验层报警。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "split_key_path",
    "greedy_literal_match",
    "phantom_sibling",
    "join_key_path",
    "get_nested",
    "has_nested",
    "format_path",
]


def split_key_path(key: str) -> list[str]:
    """把点分键路径拆成段，尊重反斜杠转义的 ``.``（``a\\.b`` → ``a.b``）。

    反斜杠出现在其他字符前时原样保留。
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(key):
        ch = key[i]
        if ch == "\\" and key[i + 1 : i + 2] == ".":
            current.append(".")
            i += 2
            continue
        if ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def join_key_path(parts: list[str] | tuple[str, ...]) -> str:
    """把段列表拼回点分路径，段内字面点会被转义。"""
    return ".".join(p.replace(".", "\\.") for p in parts)


def greedy_literal_match(container: Any, parts: list[str]) -> tuple[str, int] | None:
    """在 *container* 中查找最长的「字面点键」，返回 ``(键, 消耗段数)``。

    没有多段字面键时返回 ``None``，调用方退回普通逐段拆分。
    """
    if not isinstance(container, dict) or not parts:
        return None
    for n in range(len(parts), 0, -1):
        candidate = ".".join(parts[:n])
        if candidate in container:
            return candidate, n
    return None


def phantom_sibling(container: Any, part: str) -> str | None:
    """若要新建中间映射 *part*，返回会被它遮蔽的既有字面点键（否则 ``None``）。

    例：同级已有 ``grok-4.5``，此时创建 ``grok-4`` 映射会生成运行时不读的幻影节点。
    """
    if not isinstance(container, dict):
        return None
    prefix = part + "."
    for key in container:
        if isinstance(key, str) and key.startswith(prefix):
            return key
    return None


def get_nested(data: Any, key_path: str) -> tuple[bool, Any]:
    """读取点分键路径，返回 ``(是否存在, 值)``。

    导航规则与写入一致（含最长字面点键优先），因此「读到的」与「写到的」永远是同一位置。
    """
    parts = split_key_path(key_path)
    current: Any = data
    i = 0
    while i < len(parts):
        if not isinstance(current, dict):
            return False, None
        remaining = parts[i:]
        match = greedy_literal_match(current, remaining)
        segment, consumed = match if match else (remaining[0], 1)
        if segment not in current:
            return False, None
        current = current[segment]
        i += consumed
    return True, current


def has_nested(data: Any, key_path: str) -> bool:
    return get_nested(data, key_path)[0]


def format_path(parts: list[str] | tuple[str, ...]) -> str:
    """人类可读的路径表示（用于差异预览与冲突报告）。"""
    return " → ".join(parts)
