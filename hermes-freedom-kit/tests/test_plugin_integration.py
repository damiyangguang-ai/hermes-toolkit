"""插件数据层 ↔ 本地服务 ↔ 补丁引擎 的真实链路测试。

跑的是 ``scripts/plugin_e2e.py``：Node 侧用插件里真实的 HTTP 客户端，打真实的
``ThreadingHTTPServer``，驱动真实的 apply/restore，落盘到沙箱里一份真实配置副本。
没有任何一层被 mock。

缺 node、或缺真实 config.yaml 时自动 skip（不制造假失败）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SCRIPT = KIT / "scripts" / "plugin_e2e.py"
def _default_hermes_home() -> Path | None:
    """按环境推导 Hermes Home，避免把本机路径写死进仓库。

    优先级：``HFK_HERMES_HOME`` → ``LOCALAPPDATA/hermes`` → None。
    推导不到时返回 None，测试按 skip 处理（不制造假失败）。
    """
    override = os.environ.get("HFK_HERMES_HOME")
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "hermes"
        return candidate if candidate.exists() else None
    return None


_HERMES_HOME = _default_hermes_home()
LIVE_CONFIG = (_HERMES_HOME / "config.yaml") if _HERMES_HOME else Path("__no_hermes_home__")
NODE_CANDIDATES = (
    r"<DSH_HOME>\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
    "node",
)


def _find_node() -> str | None:
    for candidate in NODE_CANDIDATES:
        if Path(candidate).is_absolute():
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


@unittest.skipUnless(SCRIPT.exists(), "缺少 scripts/plugin_e2e.py")
@unittest.skipUnless(_find_node() is not None, "缺少 node")
@unittest.skipUnless(LIVE_CONFIG.exists(), "缺少真实 config.yaml 作为样本")
class TestPluginIntegration(unittest.TestCase):
    def test_plugin_client_drives_real_service(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(KIT)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=str(KIT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            env=env,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        self.assertEqual(
            completed.returncode,
            0,
            "插件数据层真实链路测试未通过：\n" + output[-3000:],
        )
        self.assertIn("全部通过", output)
        self.assertIn("最终配置与初始一致：True", output)


if __name__ == "__main__":
    unittest.main()
