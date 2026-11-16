"""构建「沙箱 Hermes 环境」供自检与自动化测试使用。

真正的测试不能拿用户的实时配置开刀。这里合成一个最小但**真实**的 Hermes 布局：

    <root>/home/config.yaml        ← 带注释、带 flow 列表、CRLF 的用户式配置
    <root>/install/hermes_cli/     ← 让安装目录识别成立
    <root>/install/install-stamp.json

版本通过 install-stamp.json 注入，因此不需要真的装有某个 Hermes 版本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Sandbox", "make_sandbox", "SAMPLE_CONFIG"]

#: 故意包含注释、flow 风格列表、带引号的 'off'、以及嵌套结构，
#: 用来验证「只改目标字段、其余原样保留」。
SAMPLE_CONFIG = """\
# Hermes 用户配置（HFK 测试样本）
# 这一行注释必须原样保留

model:
  default: test-model
  provider: test-provider

# ── Approvals ─────────────────────────────────────────
approvals:
  mode: smart
  timeout: 60
  cron_mode: deny
  destructive_slash_confirm: false
  mcp_reload_confirm: true

providers:
  - name: alpha
    base_url: http://127.0.0.1:1234
  - name: beta
    base_url: http://127.0.0.1:5678

toolsets:
  enabled: [terminal, files]

# 结尾注释
"""


@dataclass(frozen=True)
class Sandbox:
    root: Path
    home: Path
    install_dir: Path
    config_path: Path

    def env(self):
        from hfk.env import detect

        return detect(home=str(self.home), install_dir=str(self.install_dir))


def make_sandbox(
    root: str | Path,
    *,
    version: str = "0.23.1",
    config_text: str | None = None,
    newline: str = "\r\n",
) -> Sandbox:
    """在 *root* 下建立一个可写的沙箱 Hermes 环境。"""
    root = Path(root)
    home = root / "home"
    install_dir = root / "install"
    (install_dir / "hermes_cli").mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    config_path = home / "config.yaml"
    text = config_text if config_text is not None else SAMPLE_CONFIG
    payload = text.replace("\r\n", "\n").replace("\n", newline) if newline != "\n" else text
    config_path.write_bytes(payload.encode("utf-8"))

    stamp = {
        "schemaVersion": 2,
        "commit": "0" * 40,
        "baseVersion": version,
        "displayVersion": version,
        "dirty": False,
        "source": "sandbox",
    }
    (install_dir / "install-stamp.json").write_text(
        json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 让 install-stamp 的 commit 与「无 .git」共存：不创建 .git，git 探测自然返回 None
    return Sandbox(root=root, home=home, install_dir=install_dir, config_path=config_path)
