"""HFK 异常体系。

设计原则：每一类失败都有独立类型，调用方（CLI / 服务 / 插件）不需要解析错误字符串
就能区分「环境没找到」「版本不兼容」「配置读不出」「写入被拒」「事务冲突」。
所有异常都携带机器可读的 ``code``，供 UI 直接映射提示文案。
"""

from __future__ import annotations


class HFKError(Exception):
    """HFK 所有错误的基类。"""

    code = "hfk_error"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        data = {"code": self.code, "message": self.message}
        if self.detail:
            data["detail"] = self.detail
        return data


class EnvironmentNotFoundError(HFKError):
    """没有找到可用的 Hermes 安装 / Hermes Home。"""

    code = "environment_not_found"


class UnsupportedVersionError(HFKError):
    """检测到的 Hermes 版本不在兼容清单内：只允许查看与导出，禁止 Apply。"""

    code = "unsupported_version"


class ConfigReadError(HFKError):
    """配置文件读不出、或解析失败。此时绝不允许写入。"""

    code = "config_read_error"


class ConfigWriteError(HFKError):
    """配置写入失败。原始文件应保持调用前状态。"""

    code = "config_write_error"


class ConfigLockedError(ConfigWriteError):
    """配置文件被其他进程占用（Windows 上常见于重命名争用）。"""

    code = "config_locked"


class ConfigValidationError(HFKError):
    """补丁项非法：字段不存在、取值不在允许集合、类型不符等。"""

    code = "config_validation_error"


class BackupError(HFKError):
    """备份创建或校验失败。备份损坏时禁止继续任何覆盖操作。"""

    code = "backup_error"


class TransactionError(HFKError):
    """事务记录读写失败，或事务状态不允许该操作。"""

    code = "transaction_error"


class ConflictError(HFKError):
    """检测到用户后续修改，拒绝覆盖。"""

    code = "conflict_error"


class PathTraversalError(HFKError):
    """路径越界：目标不在允许的 Hermes Home 子树内。"""

    code = "path_traversal"


class SignatureError(ConflictError):
    """文件内容在读取之后、写入之前发生变化（并发修改）。"""

    code = "signature_mismatch"
