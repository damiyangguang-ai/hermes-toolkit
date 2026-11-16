"""开发期探针：从 Hermes 运行时导出与审批/确认相关的全部配置路径。

比读缩进可靠：直接导入 Hermes 的 DEFAULT_CONFIG 并遍历，输出
`dotted.path | type | default`，用于构建 HFK 的「已验证字段清单」。

运行（cwd = Hermes 仓库根，解释器 = Hermes venv）：
    python probe_schema.py > schema_dump.txt
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.getcwd())

from hermes_cli.config_defaults import DEFAULT_CONFIG  # noqa: E402

PATTERN = re.compile(r"approv|confirm|yolo|allowlist|deny|auto_accept", re.I)


def walk(node, prefix=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*prefix, str(key)))
    elif isinstance(node, list) and len(node) <= 3 and all(
        isinstance(item, (str, int, float, bool)) for item in node
    ):
        yield prefix, node
    else:
        yield prefix, node


def describe(value):
    if isinstance(value, bool):
        return f"bool default={value}"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__} default={value}"
    if isinstance(value, str):
        return f"str default={value!r}"
    if isinstance(value, list):
        return f"list[{len(value)}] default={json.dumps(value, ensure_ascii=False)}"
    if isinstance(value, dict):
        return f"dict keys={list(value)[:12]}"
    return f"{type(value).__name__} default={value!r}"


def main() -> int:
    rows = []
    for path, value in walk(DEFAULT_CONFIG):
        dotted = ".".join(path)
        if PATTERN.search(dotted):
            rows.append((dotted, describe(value)))
    rows.sort()
    print(f"# 匹配 {len(rows)} 条路径")
    for dotted, desc in rows:
        print(f"{dotted} | {desc}")

    print()
    print("# ---- approvals 子树完整结构 ----")
    print(json.dumps(DEFAULT_CONFIG.get("approvals"), ensure_ascii=False, indent=2, default=str))
    print()
    print("# ---- command_allowlist 默认值 ----")
    print(json.dumps(DEFAULT_CONFIG.get("command_allowlist"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
