"""事务存储：字段级变更的持久记录，支持多次 Apply 与**逆序**恢复。

为什么必须字段级：文档明确要求「不能只保存整个配置文件的前后版本而不记录字段级变化」。
因为恢复时唯一安全的判据是——**当前值是否仍等于 HFK 写下的值**：

  * 仍等于 → 说明用户没动过，可以还原为 before；
  * 已不同 → 说明用户后来改过，必须保留用户的值并标记冲突。

整文件覆盖无法做这个判断，会把用户的后续修改一起冲掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from hfk.errors import TransactionError
from hfk.util import json_dump_atomic, utc_now_iso, to_plain

__all__ = [
    "Change",
    "Transaction",
    "TransactionStore",
    "count_pending",
    "STATUS_APPLIED",
    "STATUS_RESTORED",
    "STATUS_PARTIAL",
    "STATUS_FAILED",
]

SCHEMA_VERSION = 1

STATUS_APPLIED = "applied"
STATUS_RESTORED = "restored"
STATUS_PARTIAL = "partially_restored"
STATUS_FAILED = "failed"

ChangeStatus = Literal["applied", "restored", "conflict", "skipped", "failed"]


@dataclass
class Change:
    """一个字段的一次修改。"""

    path: str
    before_exists: bool
    before: Any
    after: Any
    status: str = STATUS_APPLIED
    #: 写入时由 HFK 新建的中间父级映射（点分路径，由浅到深）；恢复时若变空则清理
    created_parents: list[str] = field(default_factory=list)
    #: 恢复/冲突时的人类可读说明
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before_exists": self.before_exists,
            "before": to_plain(self.before),
            "after": to_plain(self.after),
            "status": self.status,
            "created_parents": list(self.created_parents),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Change":
        return cls(
            path=str(data["path"]),
            before_exists=bool(data["before_exists"]),
            before=data.get("before"),
            after=data.get("after"),
            status=str(data.get("status", STATUS_APPLIED)),
            created_parents=[str(item) for item in data.get("created_parents") or []],
            note=str(data.get("note", "")),
        )


@dataclass
class Transaction:
    """一次 Apply 的完整记录。"""

    transaction_id: str
    hfk_version: str
    hermes_version: str
    profile: str
    timestamp: str
    status: str
    config_path: str
    before_sha256: str
    after_sha256: str
    backup_path: str
    backup_sha256: str
    preset: str = "custom"
    changes: list[Change] = field(default_factory=list)
    applied_at: str = ""
    restored_at: str = ""
    note: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "hfk_version": self.hfk_version,
            "hermes_version": self.hermes_version,
            "profile": self.profile,
            "timestamp": self.timestamp,
            "status": self.status,
            "preset": self.preset,
            "config_path": self.config_path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "backup_path": self.backup_path,
            "backup_sha256": self.backup_sha256,
            "changes": [change.to_dict() for change in self.changes],
            "applied_at": self.applied_at,
            "restored_at": self.restored_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        return cls(
            transaction_id=str(data["transaction_id"]),
            hfk_version=str(data.get("hfk_version", "")),
            hermes_version=str(data.get("hermes_version", "")),
            profile=str(data.get("profile", "default")),
            timestamp=str(data.get("timestamp", "")),
            status=str(data.get("status", STATUS_APPLIED)),
            config_path=str(data.get("config_path", "")),
            before_sha256=str(data.get("before_sha256", "")),
            after_sha256=str(data.get("after_sha256", "")),
            backup_path=str(data.get("backup_path", "")),
            backup_sha256=str(data.get("backup_sha256", "")),
            preset=str(data.get("preset", "custom")),
            changes=[Change.from_dict(item) for item in data.get("changes") or []],
            applied_at=str(data.get("applied_at", "")),
            restored_at=str(data.get("restored_at", "")),
            note=str(data.get("note", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_APPLIED


class TransactionStore:
    """``<hfk>/transactions/transactions.json`` 的读写封装。

    写入整体原子替换；读坏文件不静默吞掉——直接报错，避免「以为恢复了其实没记录」。
    """

    def __init__(self, hfk_dir: str | Path) -> None:
        self.hfk_dir = Path(hfk_dir)
        self.path = self.hfk_dir / "transactions" / "transactions.json"

    # -- 读 -----------------------------------------------------------------
    def load(self) -> list[Transaction]:
        """读取全部事务。

        文件**不存在**＝还没有事务，返回空列表；
        文件**存在但读不出/损坏**＝必须报错，绝不能当成「没有事务」——
        否则未完成的补丁会被悄悄遗忘，用户以为已经恢复其实没有。
        """
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionError(
                f"事务记录无法读取，已中止以免丢失历史：{self.path}",
                detail=str(exc),
            ) from exc
        if not isinstance(data, dict):
            raise TransactionError(f"事务文件格式不正确（顶层不是对象）：{self.path}")
        transactions = []
        for item in data.get("transactions") or []:
            try:
                transactions.append(Transaction.from_dict(item))
            except (KeyError, TypeError, ValueError):
                # 单条坏记录不应让整个历史不可读，但也绝不丢弃：保留原样在文件里。
                continue
        return transactions

    def pending(self) -> list[Transaction]:
        return [tx for tx in self.load() if tx.is_pending]

    def get(self, transaction_id: str) -> Transaction | None:
        for tx in self.load():
            if tx.transaction_id == transaction_id:
                return tx
        return None

    # -- 写 -----------------------------------------------------------------
    def _save(self, transactions: list[Transaction]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "transactions": [tx.to_dict() for tx in transactions],
        }
        json_dump_atomic(self.path, payload)

    def append(self, transaction: Transaction) -> None:
        transactions = self.load()
        if any(tx.transaction_id == transaction.transaction_id for tx in transactions):
            raise ValueError(f"事务 ID 重复：{transaction.transaction_id}")
        transactions.append(transaction)
        self._save(transactions)

    def update(self, transaction: Transaction) -> None:
        transactions = self.load()
        for index, existing in enumerate(transactions):
            if existing.transaction_id == transaction.transaction_id:
                transactions[index] = transaction
                self._save(transactions)
                return
        raise ValueError(f"事务不存在，无法更新：{transaction.transaction_id}")


def count_pending(hfk_dir: str | Path) -> int:
    """未完成事务数（环境探测用；任何异常都按 0 处理，不影响主流程）。"""
    try:
        return len(TransactionStore(hfk_dir).pending())
    except Exception:
        return 0
