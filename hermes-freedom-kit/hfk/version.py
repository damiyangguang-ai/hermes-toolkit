"""HFK 版本常量。

版本号只有一处定义，其余模块一律从这里引用，避免出现「两个版本号」。
"""

from __future__ import annotations

HFK_NAME = "HFK"
HFK_LONG_NAME = "Hermes Freedom Kit"
HFK_VERSION = "0.1.0"

#: 兼容的补丁数据结构版本（与 HFK 自身版本解耦：结构不变就不递增）
TRANSACTION_SCHEMA_VERSION = 1
