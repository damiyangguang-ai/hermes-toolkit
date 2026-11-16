"""与 Hermes 自身写入器的**字节一致性**测试（最重要的一条不变量）。

它回答的问题是：HFK 写出来的 config.yaml，是否和 Hermes 自己写出来的完全一样？
如果不一样，那么每次补丁都会产生整文件级 diff，「字段级修改」的承诺就是假的。

实现方式：调用 ``scripts/parity_probe.py``，在 Hermes 仓库根目录下用 Hermes 自己的
venv 解释器运行，让它分别用 HFK 的写入器和 Hermes 的 ``utils.atomic_roundtrip_yaml_update``
改同一个字段，然后逐字节比较。

Hermes 仓库或它的 venv 不存在时，本测试自动 skip（不制造假失败）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PROBE = KIT / "scripts" / "parity_probe.py"
def _default_hermes_repo() -> Path:
    """按环境推导 Hermes 仓库位置，避免把本机路径写死进仓库。

    优先级：``HFK_HERMES_REPO`` → ``LOCALAPPDATA/hermes/hermes-agent`` → 占位路径。
    推导不到时占位路径必然不存在，测试自动 skip（不制造假失败）。
    """
    override = os.environ.get("HFK_HERMES_REPO")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes" / "hermes-agent"
    return Path("__no_hermes_repo__")


HERMES_REPO = _default_hermes_repo()
HERMES_PY = HERMES_REPO / "venv" / "Scripts" / "python.exe"
LIVE_CONFIG = HERMES_REPO.parent / "config.yaml"


@unittest.skipUnless(PROBE.exists(), "缺少 scripts/parity_probe.py")
@unittest.skipUnless(HERMES_PY.exists(), "缺少 Hermes venv 解释器（无法做对照）")
@unittest.skipUnless((HERMES_REPO / "utils.py").exists(), "缺少 Hermes 仓库")
@unittest.skipUnless(LIVE_CONFIG.exists(), "缺少真实 config.yaml 供取样")
class TestByteParityWithHermes(unittest.TestCase):
    def test_output_is_byte_identical_to_hermes_writer(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(KIT)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [str(HERMES_PY), str(PROBE)],
            cwd=str(HERMES_REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=env,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        self.assertEqual(
            completed.returncode,
            0,
            "与 Hermes 写入器的一致性验证未通过：\n" + output[-3000:],
        )
        self.assertIn("与 Hermes 写入器字节完全一致", output)


if __name__ == "__main__":
    unittest.main()
