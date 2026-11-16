"""备份管理：字节级原始备份 + 写入后校验。

规则（来自文档「异常处理」一节）：
  * Apply 之前必须已经拥有**字节级**原始备份（不是「再序列化一份」）；
  * 备份写完要回读校验哈希，校验不过就不允许继续覆盖操作；
  * 恢复失败**不得删除**原始备份；
  * Uninstall 也**保留**所有原始备份。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from hfk.errors import BackupError
from hfk.util import ensure_dir, sha256_file, utc_now_iso

__all__ = ["BackupRecord", "create_backup", "verify_backup", "list_backups"]


@dataclass(frozen=True)
class BackupRecord:
    """一次备份的事实记录。"""

    backup_id: str
    path: Path
    sha256: str
    size: int
    created_at: str
    source_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.backup_id,
            "path": str(self.path),
            "sha256": self.sha256,
            "size": self.size,
            "created_at": self.created_at,
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackupRecord":
        return cls(
            backup_id=str(data["backup_id"]),
            path=Path(data["path"]),
            sha256=str(data["sha256"]),
            size=int(data.get("size", 0)),
            created_at=str(data.get("created_at", "")),
            source_path=str(data.get("source_path", "")),
        )


def _new_backup_id() -> str:
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"{stamp}-{secrets.token_hex(3)}"


def create_backup(config_path: str | Path, backups_dir: str | Path) -> BackupRecord:
    """把 ``config_path`` 的**原始字节**复制到 ``backups_dir``，并回读校验。"""
    config_path = Path(config_path)
    backups_dir = Path(backups_dir)
    if not config_path.exists():
        raise BackupError(f"要备份的配置文件不存在：{config_path}")
    try:
        payload = config_path.read_bytes()
    except OSError as exc:
        raise BackupError(f"读取配置失败，无法备份：{exc}") from exc

    ensure_dir(backups_dir)
    backup_id = _new_backup_id()
    destination = backups_dir / f"config-{backup_id}.yaml"
    if destination.exists():  # 极小概率的 ID 碰撞
        raise BackupError(f"备份目标已存在，拒绝覆盖：{destination}")

    try:
        with open(destination, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupError(f"写入备份失败：{exc}") from exc

    expected = sha256_file(config_path)
    actual = sha256_file(destination)
    if expected != actual:
        raise BackupError(
            "备份校验失败：备份文件哈希与源文件不一致，已中止（不会继续任何覆盖操作）",
            detail=f"source={expected} backup={actual} path={destination}",
        )

    record = BackupRecord(
        backup_id=backup_id,
        path=destination,
        sha256=actual,
        size=len(payload),
        created_at=utc_now_iso(),
        source_path=str(config_path),
    )
    return record


def verify_backup(record: BackupRecord) -> bool:
    """备份是否仍然完好（存在且哈希一致）。损坏时禁止继续覆盖。"""
    try:
        if not record.path.exists():
            return False
        return sha256_file(record.path) == record.sha256
    except OSError:
        return False


def list_backups(backups_dir: str | Path) -> list[Path]:
    directory = Path(backups_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("config-*.yaml"), key=lambda item: item.name, reverse=True)


def read_backup_bytes(record: BackupRecord) -> bytes:
    """读取备份内容；哈希不符则抛错（绝不拿一份坏备份去覆盖）。"""
    if not verify_backup(record):
        raise BackupError(
            f"备份已损坏或缺失，拒绝使用：{record.path}",
            detail=f"期望哈希 {record.sha256}",
        )
    return record.path.read_bytes()
