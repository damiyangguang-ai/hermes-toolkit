"""插件数据层 + 本地服务 + 补丁引擎的**真实**端到端测试。

链路：Node（插件里的 HTTP 客户端）→ 真实 HTTP → 真实 HFK Core 服务 → 真实补丁引擎
→ 沙箱里一份**真实 config.yaml 的副本**。

没有任何一层被 mock：
  * Node 侧不是模拟 fetch，走的是真 http；
  * 服务是真的 ThreadingHTTPServer，带真令牌校验；
  * 引擎是真的 apply/restore，真的写 YAML、真的备份。

运行：
    python scripts/plugin_e2e.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PLUGIN = KIT / "desktop-plugin" / "plugin.js"
CLIENTTEST = KIT / "desktop-plugin" / "plugin.clienttest.mjs"


def _live_config() -> Path:
    """定位真实 config.yaml：环境变量优先，其次按 LOCALAPPDATA 推导。

    不写死本机路径——否则仓库在别人机器上必然失败（这是真实踩过的坑）。
    """
    override = os.environ.get("HFK_HERMES_HOME")
    if override:
        return Path(override) / "config.yaml"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes" / "config.yaml"
    return Path("__no_hermes_home__") / "config.yaml"


LIVE_CONFIG = _live_config()

NODE_CANDIDATES = (
    "node",
    "node.exe",
)

sys.path.insert(0, str(KIT))

from hfk.facade import HFK  # noqa: E402
from hfk.service.app import HFKService  # noqa: E402
from hfk.testing import make_sandbox  # noqa: E402


def find_node() -> str | None:
    for candidate in NODE_CANDIDATES:
        path = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
        if path and Path(path).exists():
            return path
    return None


def main() -> int:
    node = find_node()
    if node is None:
        print("跳过：没有找到 node")
        return 0
    if not PLUGIN.exists() or not CLIENTTEST.exists():
        print("失败：缺少 plugin.js 或 plugin.clienttest.mjs")
        return 1

    work = Path(tempfile.mkdtemp(prefix="hfk-plugin-e2e-"))
    try:
        box = make_sandbox(work / "sandbox")
        # 用真实配置副本，确保断言里的 60 秒超时等真实存在
        shutil.copyfile(LIVE_CONFIG, box.config_path)
        before = box.config_path.read_bytes()
        print(f"沙箱配置：{box.config_path}（{len(before)} 字节，真实配置副本）")

        hfk = HFK(box.env())
        service = HFKService(hfk)
        service.start()
        print(f"真实服务：{service.base_url}（令牌长度 {len(service.token)}）")
        try:
            completed = subprocess.run(
                [node, str(CLIENTTEST), str(PLUGIN), service.base_url, service.token],
                cwd=str(KIT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            print(completed.stdout or "")
            if completed.stderr.strip():
                print("[node stderr]")
                print(completed.stderr.strip()[-2000:])
            code = completed.returncode
        finally:
            service.stop()

        after = box.config_path.read_bytes()
        print("")
        print(f"最终配置与初始一致：{after == before}")
        if after != before:
            print("  注意：测试末尾未恢复（restore 后应完全一致）")
            return 1
        return code
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
