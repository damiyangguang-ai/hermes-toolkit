"""验证所有模块可导入（骨架完整性检查）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
modules = []
for path in sorted((root / "hfk").rglob("*.py")):
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts:
        modules.append(".".join(parts))

ok, bad = 0, []
for name in modules:
    try:
        importlib.import_module(name)
        ok += 1
    except Exception as exc:  # noqa: BLE001
        bad.append((name, repr(exc)))

print(f"模块 {len(modules)} 个，导入成功 {ok} 个，失败 {len(bad)} 个")
for name, error in bad:
    print(f"  FAIL {name}: {error}")
sys.exit(1 if bad else 0)
