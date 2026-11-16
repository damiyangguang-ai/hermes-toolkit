"""补丁引擎：Preview / Apply / Restore / Uninstall / Status / 冲突检测。

这是 HFK 真正的核心。三条不可违反的规则：

1. **写入前必须有字节级备份**，且备份回读校验通过；
2. **只做字段级修改**（一次加载、一次原子替换），绝不用整份配置覆盖；
3. **恢复以「当前值是否仍等于 HFK 写下的值」为唯一判据**——用户后来改过就保留用户的值。

失败路径同样是设计的一部分：任何中途异常都用备份**原始字节**回滚，并把事务标记为 failed，
绝不把「没做完」记录成「做完了」。
"""

from __future__ import annotations

import secrets

from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from hfk.compat import CompatResult, check as compat_check
from hfk.config.keypath import get_nested, split_key_path, greedy_literal_match
from hfk.config.yaml_io import (
    atomic_write_bytes,
    plain_yaml,
    read_text,
    update_keys,
)
from hfk.engine.backup import BackupRecord, create_backup, read_backup_bytes, verify_backup
from hfk.engine.transaction import (
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_RESTORED,
    Change,
    Transaction,
    TransactionStore,
)
from hfk.env import HermesEnvironment
from hfk.errors import (
    BackupError,
    ConfigReadError,
    ConfigValidationError,
    ConfigWriteError,
    SignatureError,
    UnsupportedVersionError,
)
from hfk.schema import get_spec, validate_value
from hfk.util import ensure_dir, sha256_file, utc_now_iso
from hfk.version import HFK_VERSION

__all__ = [
    "PlannedChange",
    "PatchPlan",
    "RestoreConflict",
    "RestoreReport",
    "UninstallReport",
    "PatchStatusView",
    "build_plan",
    "apply",
    "restore",
    "uninstall",
    "status",
]

# --------------------------------------------------------------------------- 数据结构


@dataclass
class PlannedChange:
    path: str
    title: str
    before_exists: bool
    before: Any
    after: Any
    changed: bool
    risk: str = "A"
    effect: str = ""
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "before_exists": self.before_exists,
            "before": self.before,
            "after": self.after,
            "changed": self.changed,
            "risk": self.risk,
            "effect": self.effect,
            "error": self.error,
            "note": self.note,
            "display": self.display(),
        }

    def display(self) -> str:
        """``当前值 → 计划值`` 的人话表示。"""
        current = "（未设置）" if not self.before_exists else _human(self.before)
        text = f"{current} → {_human(self.after)}"
        if self.note and not self.changed:
            text += f"   {self.note}"
        return text


@dataclass
class PatchPlan:
    """一次补丁的完整预演，包含每个字段的 before/after 与错误。"""

    config_path: str
    expected_sha256: str
    preset: str
    changes: list[PlannedChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def writable(self) -> dict[str, Any]:
        """真正需要写入的字段（已排除无变化项与错误项）。"""
        return {
            change.path: change.after
            for change in self.changes
            if change.changed and not change.error
        }

    @property
    def has_effect(self) -> bool:
        return bool(self.writable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "expected_sha256": self.expected_sha256,
            "preset": self.preset,
            "has_effect": self.has_effect,
            "changes": [change.to_dict() for change in self.changes],
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class RestoreConflict:
    path: str
    current_exists: bool
    current: Any
    hfk_wrote: Any
    original: Any
    original_existed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "current_exists": self.current_exists,
            "current": self.current,
            "hfk_wrote": self.hfk_wrote,
            "original": self.original,
            "original_existed": self.original_existed,
            "reason": self.reason,
        }


@dataclass
class RestoreReport:
    transaction_ids: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    conflicts: list[RestoreConflict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = STATUS_RESTORED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_ids": self.transaction_ids,
            "restored": self.restored,
            "conflicts": [item.to_dict() for item in self.conflicts],
            "skipped": self.skipped,
            "warnings": self.warnings,
            "status": self.status,
        }


@dataclass
class UninstallReport:
    restore: RestoreReport = field(default_factory=RestoreReport)
    removed_plugin: bool = False
    kept_backups: int = 0
    record_path: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "restore": self.restore.to_dict(),
            "removed_plugin": self.removed_plugin,
            "kept_backups": self.kept_backups,
            "record_path": self.record_path,
            "notes": self.notes,
        }


@dataclass
class PatchStatusView:
    hfk_version: str
    hermes_version: str | None
    supported: bool
    compat_reason: str
    profile: str
    config_path: str
    pending_transactions: int
    total_transactions: int
    backups: int
    last_apply: str | None
    last_restore: str | None
    active_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hfk_version": self.hfk_version,
            "hermes_version": self.hermes_version,
            "supported": self.supported,
            "compat_reason": self.compat_reason,
            "profile": self.profile,
            "config_path": self.config_path,
            "pending_transactions": self.pending_transactions,
            "total_transactions": self.total_transactions,
            "backups": self.backups,
            "last_apply": self.last_apply,
            "last_restore": self.last_restore,
            "active_fields": self.active_fields,
        }


# --------------------------------------------------------------------------- 内部工具


def _human(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]" if value else "[]"
    return str(value)


def _same(left: Any, right: Any) -> bool:
    """严格相等：``True == 1`` 在 Python 成立，但对 YAML 读者是两种值。"""
    if isinstance(left, bool) is not isinstance(right, bool):
        return False
    return left == right


def _load_plain(env: HermesEnvironment) -> dict[str, Any]:
    """读取当前配置（只读、YAML 1.1 语义）；失败即拒绝写入。"""
    if not env.config_path.exists():
        raise ConfigReadError(f"配置文件不存在：{env.config_path}")
    try:
        text = read_text(env.config_path)
    except OSError as exc:
        raise ConfigReadError(f"配置文件无法读取：{exc}") from exc
    try:
        document = plain_yaml().load(text) if text.strip() else {}
    except Exception as exc:
        raise ConfigReadError(f"配置文件解析失败，已中止（不会写入）：{exc}") from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConfigReadError(
            f"配置顶层不是映射（是 {type(document).__name__}），拒绝写入"
        )
    return document


def _chain(document: Any, key_path: str) -> list[dict[str, Any]]:
    """沿点分路径逐段行走，返回每一段的 (路径, 是否存在, 值, 是否映射)。

    导航规则与写入完全一致（含最长字面点键优先），因此「检查的」就是「会写的」。
    """
    parts = split_key_path(key_path)
    chain: list[dict[str, Any]] = []
    current: Any = document
    index = 0
    while index < len(parts):
        remaining = parts[index:]
        present = isinstance(current, dict)
        match = greedy_literal_match(current, remaining) if present else None
        segment, consumed = match if match else (remaining[0], 1)
        exists = present and segment in current
        value = current.get(segment) if exists else None
        chain.append(
            {
                "dotted": ".".join(parts[: index + consumed]),
                "present": exists,
                "value": value,
                "is_mapping": isinstance(value, dict),
                "last": index + consumed == len(parts),
            }
        )
        if index + consumed == len(parts):
            break
        if not exists or not isinstance(value, dict):
            current = {}  # 后续层级都将被创建
        else:
            current = value
        index += consumed
    return chain


def _created_parents(document: Any, key_path: str) -> list[str]:
    """写入该键时会被 HFK 新建的中间父级路径（由浅到深）。"""
    return [entry["dotted"] for entry in _chain(document, key_path)[:-1] if not entry["present"]]


def _refuse_scalar_parents(document: Any, key_path: str) -> None:
    """父级存在但不是映射时拒绝写入。

    Hermes 自己的写入器会把标量父级直接替换成映射（静默丢掉原值）。
    HFK 不这么做：拒绝，并让用户先手动修正配置。
    """
    for entry in _chain(document, key_path)[:-1]:
        if entry["present"] and not entry["is_mapping"]:
            raise ConfigValidationError(
                f"拒绝写入 {key_path}：父级 {entry['dotted']} 已存在但不是映射"
                f"（当前值 {_human(entry['value'])}），写入会丢失它"
            )


def _store(env: HermesEnvironment) -> TransactionStore:
    return TransactionStore(env.hfk_dir)


def _assert_writable(env: HermesEnvironment) -> CompatResult:
    """版本与配置可读性双重闸门。"""
    result = compat_check(env.version_tuple, env.version)
    if not result.supported:
        raise UnsupportedVersionError(result.reason)
    if not env.config_readable:
        raise ConfigReadError(env.config_error or "配置文件不可读，拒绝写入")
    return result


# --------------------------------------------------------------------------- Preview


def build_plan(
    env: HermesEnvironment,
    items: dict[str, Any],
    *,
    preset: str = "custom",
) -> PatchPlan:
    """对比目标值与当前值，生成可展示、可校验的计划。"""
    document = _load_plain(env)
    plan = PatchPlan(
        config_path=str(env.config_path),
        expected_sha256=sha256_file(env.config_path),
        preset=preset,
    )

    for path, target in items.items():
        spec = get_spec(path)
        title = spec.title if spec else path
        errors = validate_value(path, target)
        exists, current = get_nested(document, path)

        if errors:
            plan.changes.append(
                PlannedChange(
                    path=path,
                    title=title,
                    before_exists=exists,
                    before=current,
                    after=target,
                    changed=False,
                    risk=spec.risk if spec else "C",
                    effect=spec.effect if spec else "",
                    error="; ".join(errors),
                )
            )
            plan.errors.extend(errors)
            continue

        try:
            _refuse_scalar_parents(document, path)
        except ConfigValidationError as exc:
            plan.changes.append(
                PlannedChange(
                    path=path,
                    title=title,
                    before_exists=exists,
                    before=current,
                    after=target,
                    changed=False,
                    risk=spec.risk if spec else "C",
                    effect=spec.effect if spec else "",
                    error=exc.message,
                )
            )
            plan.errors.append(exc.message)
            continue

        changed = not (exists and _same(current, target))
        note = ""
        # 与 Hermes 自己的策略一致：**默认值不落盘**。
        # 若该键当前缺失、而目标值恰好等于 Hermes 默认值，写入不会改变任何行为，
        # 只会让 config.yaml 变脏，因此跳过并在预览里说明。
        if (
            not exists
            and changed
            and spec is not None
            and spec.kind in ("enum", "int", "bool", "str")
            and spec.default is not None
            and _same(target, spec.default)
        ):
            changed = False
            note = "（当前未设置，目标值等于 Hermes 默认值，无需写入）"

        plan.changes.append(
            PlannedChange(
                path=path,
                title=title,
                before_exists=exists,
                before=current,
                after=target,
                changed=changed,
                risk=spec.risk if spec else "C",
                effect=spec.effect if spec else "",
                note=note,
            )
        )

    if plan.changes and not any(change.changed for change in plan.changes):
        plan.warnings.append("所选字段的当前值已与目标值一致，无需修改")

    # B 类字段一律给出显式警示（绝不静默削弱防护）
    risky = [change for change in plan.changes if change.risk == "B" and change.changed]
    for change in risky:
        plan.warnings.append(
            f"{change.title}（{change.path}）属于高风险项：{change.effect}"
        )
    return plan


# --------------------------------------------------------------------------- Apply


def apply(env: HermesEnvironment, plan: PatchPlan) -> Transaction:
    """执行补丁：备份 → 字段级写入 → 校验 → 记录事务。

    失败时用备份原始字节回滚，并把事务记为 failed。
    """
    compat = _assert_writable(env)

    if plan.errors:
        raise ConfigValidationError("计划中存在错误，拒绝执行：" + "; ".join(plan.errors))
    writes = plan.writable
    if not writes:
        raise ConfigValidationError("没有需要修改的字段（全部已是目标值）")

    current_sha = sha256_file(env.config_path)
    if current_sha != plan.expected_sha256:
        raise SignatureError(
            "配置文件在预览之后发生了改变，拒绝写入。请重新预览后再应用",
            detail=f"预览时 {plan.expected_sha256}，当前 {current_sha}",
        )

    document = _load_plain(env)
    created: dict[str, list[str]] = {}
    for path in writes:
        _refuse_scalar_parents(document, path)
        created[path] = _created_parents(document, path)

    ensure_dir(env.hfk_dir)
    backup = create_backup(env.config_path, env.hfk_dir / "backups")
    if not verify_backup(backup):
        raise BackupError("备份校验未通过，拒绝继续覆盖操作")

    before_bytes = read_backup_bytes(backup)

    try:
        update_keys(env.config_path, writes)
    except Exception as exc:
        _rollback(env, before_bytes)
        _record_failure(env, plan, writes, created, backup, compat, f"写入失败：{exc}")
        raise ConfigWriteError(f"写入配置失败，已回滚到原始字节：{exc}") from exc

    mismatches = _verify_written(env, writes)
    if mismatches:
        _rollback(env, before_bytes)
        _record_failure(
            env, plan, writes, created, backup, compat,
            "写入后校验失败：" + "; ".join(mismatches),
        )
        raise ConfigWriteError(
            "写入后校验失败，已回滚到原始字节：" + "; ".join(mismatches)
        )

    after_sha = sha256_file(env.config_path)
    transaction = Transaction(
        transaction_id=f"tx-{utc_now_iso().replace(':', '').replace('-', '')}-{secrets.token_hex(4)}",
        hfk_version=HFK_VERSION,
        hermes_version=env.version or "unknown",
        profile=env.profile,
        timestamp=utc_now_iso(),
        status=STATUS_APPLIED,
        config_path=str(env.config_path),
        before_sha256=backup.sha256,
        after_sha256=after_sha,
        backup_path=str(backup.path),
        backup_sha256=backup.sha256,
        preset=plan.preset,
        applied_at=utc_now_iso(),
        changes=[
            Change(
                path=change.path,
                before_exists=change.before_exists,
                before=change.before,
                after=change.after,
                status="applied",
                created_parents=created.get(change.path, []),
            )
            for change in plan.changes
            if change.path in writes
        ],
    )
    _store(env).append(transaction)
    return transaction


def _verify_written(env: HermesEnvironment, writes: dict[str, Any]) -> list[str]:
    """回读校验：每个字段必须真的等于目标值。"""
    try:
        document = _load_plain(env)
    except ConfigReadError as exc:
        return [f"回读失败：{exc.message}"]
    problems: list[str] = []
    for path, expected in writes.items():
        exists, actual = get_nested(document, path)
        if not exists or not _same(actual, expected):
            problems.append(f"{path} 期望 {_human(expected)}，实际 {_human(actual) if exists else '缺失'}")
    return problems


def _rollback(env: HermesEnvironment, payload: bytes) -> None:
    atomic_write_bytes(env.config_path, payload)


def _record_failure(
    env: HermesEnvironment,
    plan: PatchPlan,
    writes: dict[str, Any],
    created: dict[str, list[str]],
    backup: BackupRecord,
    compat: CompatResult,
    reason: str,
) -> None:
    """失败也要留痕：事务记为 failed，备份路径保留，便于事后审计。"""
    planned = {change.path: change for change in plan.changes}
    try:
        transaction = Transaction(
            transaction_id=f"tx-{utc_now_iso().replace(':', '').replace('-', '')}-{secrets.token_hex(4)}",
            hfk_version=HFK_VERSION,
            hermes_version=env.version or "unknown",
            profile=env.profile,
            timestamp=utc_now_iso(),
            status=STATUS_FAILED,
            config_path=str(env.config_path),
            before_sha256=backup.sha256,
            after_sha256=sha256_file(env.config_path) if env.config_path.exists() else "",
            backup_path=str(backup.path),
            backup_sha256=backup.sha256,
            preset=plan.preset,
            note=reason,
            changes=[
                Change(
                    path=path,
                    before_exists=planned[path].before_exists if path in planned else False,
                    before=planned[path].before if path in planned else None,
                    after=value,
                    status="failed",
                    created_parents=created.get(path, []),
                )
                for path, value in writes.items()
            ],
        )
        _store(env).append(transaction)
    except Exception:
        # 记录失败不能掩盖原始错误
        pass


# --------------------------------------------------------------------------- Restore


def restore(env: HermesEnvironment, transaction_id: str | None = None) -> RestoreReport:
    """字段级恢复；冲突项保留用户当前值。

    不指定 ``transaction_id`` 时，按**事务逆序**恢复所有 pending 事务。
    """
    store = _store(env)
    transactions = store.load()
    pending = [tx for tx in transactions if tx.is_pending]
    if transaction_id is not None:
        target = next((tx for tx in pending if tx.transaction_id == transaction_id), None)
        if target is None:
            raise ConfigValidationError(
                f"找不到可恢复的事务 {transaction_id}（可能已恢复，或不是 pending 状态）"
            )
        targets = [target]
    else:
        targets = list(reversed(pending))

    report = RestoreReport()
    if not targets:
        report.warnings.append("没有待恢复的 HFK 事务")
        return report

    # 恢复是「回到原状」这个安全方向，因此**不因为版本未知就被禁止**——
    # 否则用户升级 Hermes 后会被卡在「补丁还在、却无法撤销」的状态。
    # 只要求配置可读（否则无法安全地按字段恢复）。
    if not env.config_readable:
        raise ConfigReadError(env.config_error or "配置文件不可读，无法安全恢复")
    compat = compat_check(env.version_tuple, env.version)
    if not compat.supported:
        report.warnings.append(
            f"当前 Hermes 版本未验证（{compat.reason}）；仍然允许恢复，但请确认恢复结果符合预期"
        )

    for transaction in targets:
        report.transaction_ids.append(transaction.transaction_id)
        backup_path = Path(transaction.backup_path)
        if not backup_path.exists():
            report.warnings.append(
                f"事务 {transaction.transaction_id} 的备份已缺失（{backup_path}），"
                "该事务不会被恢复，也不会被删除"
            )
            continue

        document = _load_plain(env)
        per_tx_conflicts = 0
        updates: dict[str, Any] = {}

        for change in reversed(transaction.changes):
            exists, current = get_nested(document, change.path)
            if exists and _same(current, change.after):
                # 当前值仍等于 HFK 写下的值 → 用户没动过，可以安全还原
                if change.before_exists:
                    updates[change.path] = change.before
                else:
                    updates[change.path] = None
                report.restored.append(change.path)
            elif not exists:
                # 字段现在不存在
                if change.after is None:
                    report.skipped.append(change.path)  # 本来就没写值，无需处理
                else:
                    per_tx_conflicts += 1
                    report.conflicts.append(
                        RestoreConflict(
                            path=change.path,
                            current_exists=False,
                            current=None,
                            hfk_wrote=change.after,
                            original=change.before,
                            original_existed=change.before_exists,
                            reason="字段已被删除（可能是你手动删的），保留删除状态",
                        )
                    )
            else:
                # 存在但不是 HFK 写的那个值 → 用户后来改过，保留用户的值
                per_tx_conflicts += 1
                report.conflicts.append(
                    RestoreConflict(
                        path=change.path,
                        current_exists=True,
                        current=current,
                        hfk_wrote=change.after,
                        original=change.before,
                        original_existed=change.before_exists,
                        reason="当前值已被修改，保留你的新值，不覆盖",
                    )
                )

        if updates:
            try:
                update_keys(env.config_path, updates)
            except Exception as exc:
                report.warnings.append(
                    f"事务 {transaction.transaction_id} 恢复写入失败：{exc}（事务保持 pending，可重试）"
                )
                continue

        # 清理 HFK 创建且已变空的父级节点
        _cleanup_created_parents(env, transaction)

        transaction.status = STATUS_PARTIAL if per_tx_conflicts else STATUS_RESTORED
        transaction.restored_at = utc_now_iso()
        if per_tx_conflicts:
            transaction.note = (
                f"恢复时发现 {per_tx_conflicts} 个字段存在用户修改，已保留用户当前值"
            )
        store.update(transaction)

    if report.conflicts:
        report.status = STATUS_PARTIAL
    elif report.restored:
        report.status = STATUS_RESTORED
    else:
        report.status = STATUS_APPLIED
    return report


def _cleanup_created_parents(env: HermesEnvironment, transaction: Transaction) -> None:
    """删除 HFK 新建、且现在已为空的父级映射；原本存在的节点绝不动。"""
    candidates: list[str] = []
    for change in transaction.changes:
        candidates.extend(change.created_parents)
    if not candidates:
        return
    try:
        document = _load_plain(env)
    except ConfigReadError:
        return
    for path in sorted(set(candidates), key=lambda item: item.count("."), reverse=True):
        exists, value = get_nested(document, path)
        if exists and isinstance(value, dict) and not value:
            try:
                update_keys(env.config_path, {path: None})
            except Exception:
                return
            try:
                document = _load_plain(env)
            except ConfigReadError:
                return


# --------------------------------------------------------------------------- Uninstall


def uninstall(env: HermesEnvironment, *, remove_plugin: bool = True) -> UninstallReport:
    """卸载 HFK：先安全恢复，再删除 HFK 自身运行文件。

    明确**不**做的事：不删 Hermes、不删用户配置、不删 SOUL.md / Skills。
    明确**保留**的东西：所有原始备份、事务历史、卸载记录。
    """
    report = UninstallReport()
    report.restore = restore(env)

    if remove_plugin:
        plugin_dir = env.home / "desktop-plugins" / "hfk"
        if plugin_dir.is_dir():
            from hfk.util import assert_within
            import shutil

            assert_within(plugin_dir, env.home, what="插件目录")
            shutil.rmtree(plugin_dir, ignore_errors=True)
            report.removed_plugin = not plugin_dir.exists()

    backups_dir = env.hfk_dir / "backups"
    if backups_dir.is_dir():
        report.kept_backups = len(list(backups_dir.glob("config-*.yaml")))

    record_path = env.hfk_dir / "uninstalled.json"
    try:
        from hfk.util import json_dump_atomic

        json_dump_atomic(
            record_path,
            {
                "uninstalled_at": utc_now_iso(),
                "hfk_version": HFK_VERSION,
                "hermes_version": env.version,
                "profile": env.profile,
                "restore": report.restore.to_dict(),
                "kept_backups": report.kept_backups,
                "note": "HFK 已卸载。原始备份与事务历史保留在此目录，供随时审计或手工回退。",
            },
        )
        report.record_path = str(record_path)
    except Exception as exc:
        report.notes.append(f"卸载记录写入失败：{exc}")

    report.notes.append("Hermes 本体、用户配置、SOUL.md 与 Skills 均未被修改")
    return report


# --------------------------------------------------------------------------- Status


def status(env: HermesEnvironment) -> PatchStatusView:
    store = _store(env)
    transactions = store.load()
    pending = [tx for tx in transactions if tx.is_pending]
    backups_dir = env.hfk_dir / "backups"
    backup_count = len(list(backups_dir.glob("config-*.yaml"))) if backups_dir.is_dir() else 0

    active: dict[str, Any] = {}
    for transaction in pending:
        for change in transaction.changes:
            active[change.path] = change.after

    applied = [tx for tx in transactions if tx.applied_at]
    restored = [tx for tx in transactions if tx.restored_at]
    compat = compat_check(env.version_tuple, env.version)

    return PatchStatusView(
        hfk_version=HFK_VERSION,
        hermes_version=env.version,
        supported=compat.supported,
        compat_reason=compat.reason,
        profile=env.profile,
        config_path=str(env.config_path),
        pending_transactions=len(pending),
        total_transactions=len(transactions),
        backups=backup_count,
        last_apply=max((tx.applied_at for tx in applied), default=None),
        last_restore=max((tx.restored_at for tx in restored), default=None),
        active_fields=active,
    )
