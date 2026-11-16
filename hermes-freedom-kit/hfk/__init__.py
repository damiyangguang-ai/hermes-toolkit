"""HFK（Hermes Freedom Kit）核心包。

HFK 是一个独立于 Hermes 主仓库的宿主补丁工具：它识别本机 Hermes 环境，
以「字段级」方式修改其审批/交互配置，并保证每次修改都可逆、可审计。

本包不导入 Hermes 的任何模块，因此 Hermes 升级或更换 Desktop 架构时，
HFK 的核心引擎不会随之失效。
"""

from hfk.version import HFK_NAME, HFK_VERSION

__all__ = ["HFK_NAME", "HFK_VERSION"]
