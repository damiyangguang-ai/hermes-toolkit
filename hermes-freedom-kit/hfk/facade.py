"""高层门面：把「环境探测 + 补丁引擎 + 预设」组装成 CLI / HTTP 服务共用的接口。

上层（命令行、本地服务、Desktop 插件）只与本模块交互，不直接碰引擎细节。
"""

from __future__ import annotations

from typing import Any

from hfk.compat import check as compat_check
from hfk.engine import patch
from hfk.engine.backup import list_backups
from hfk.engine.transaction import TransactionStore
from hfk.env import HermesEnvironment, detect
from hfk.errors import ConfigValidationError, HFKError
from hfk.profiles import (
    PRESETS,
    PRESET_ORDER,
    compose,
    diff_against,
    explain,
    get_preset,
    preset_items,
)
from hfk.schema import BLOCKED_FIELDS, CATALOGUE, CODE_LEVEL_FLOORS, DEFAULT_PRESET
from hfk.util import sha256_file
from hfk.version import HFK_LONG_NAME, HFK_VERSION

__all__ = ["HFK"]


class HFK:
    """一次 HFK 会话。"""

    def __init__(self, env: HermesEnvironment) -> None:
        self.env = env

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def connect(
        cls,
        home: str | None = None,
        install_dir: str | None = None,
    ) -> "HFK":
        return cls(detect(home=home, install_dir=install_dir))

    def refresh(self) -> "HFK":
        """重新探测环境（写入前必须刷新，避免用过期快照做判断）。"""
        self.env = detect(home=str(self.env.home), install_dir=str(self.env.install_dir) if self.env.install_dir else None)
        return self

    # -- 只读视图 -----------------------------------------------------------
    def env_report(self) -> dict[str, Any]:
        compat = compat_check(self.env.version_tuple, self.env.version)
        data = self.env.to_dict()
        data["compat"] = compat.to_dict()
        data["hfk_version"] = HFK_VERSION
        data["hfk_name"] = HFK_LONG_NAME
        return data

    def status(self) -> dict[str, Any]:
        view = patch.status(self.env)
        return view.to_dict()

    def catalogue(self) -> dict[str, Any]:
        return {
            "fields": [
                {
                    "path": spec.path,
                    "title": spec.title,
                    "kind": spec.kind,
                    "type": spec.describe_type(),
                    "values": list(spec.values),
                    "default": spec.default,
                    "risk": spec.risk,
                    "effect": spec.effect,
                    "surfaces": list(spec.surfaces),
                    "evidence": spec.evidence,
                    "notes": spec.notes,
                    "presets": spec.presets,
                }
                for spec in CATALOGUE
            ],
            "blocked": [
                {"path": path, "reason": reason} for path, reason in BLOCKED_FIELDS.items()
            ],
            "code_level_floors": [
                {"name": name, "detail": detail} for name, detail in CODE_LEVEL_FLOORS
            ],
        }

    def presets(self) -> dict[str, Any]:
        return {
            "default": DEFAULT_PRESET,
            "order": list(PRESET_ORDER),
            "items": {
                key: {
                    "label": preset.label,
                    "description": preset.description,
                    "risk_note": preset.risk_note,
                    "requires_acknowledgement": preset.requires_acknowledgement,
                    "changes": explain(preset_items(key)),
                }
                for key, preset in PRESETS.items()
            },
        }

    def transactions(self) -> dict[str, Any]:
        store = TransactionStore(self.env.hfk_dir)
        items = [tx.to_dict() for tx in store.load()]
        return {"count": len(items), "transactions": items}

    def backups(self) -> dict[str, Any]:
        directory = self.env.hfk_dir / "backups"
        files = list_backups(directory)
        return {
            "directory": str(directory),
            "count": len(files),
            "files": [{"name": item.name, "size": item.stat().st_size} for item in files],
        }

    def current_config(self) -> dict[str, Any]:
        """**仅供进程内部使用**（例如 :meth:`diff`）：完整配置文档。

        注意：绝不通过 HTTP 暴露这个方法——config.yaml 里可能有用户自己写入的
        API Key（例如 ``auxiliary.approval.api_key``）。对外一律用 :meth:`managed_config`。
        """
        from hfk.config.yaml_io import plain_yaml, read_text

        if not self.env.config_readable:
            return {"readable": False, "error": self.env.config_error}
        document = plain_yaml().load(read_text(self.env.config_path)) or {}
        return {"readable": True, "document": document}

    def managed_config(self) -> dict[str, Any]:
        """只回传 HFK 管理的那些字段的当前值（安全投影，可对外暴露）。

        即使 config.yaml 里存在 API Key 等秘密，它们也不会出现在这里，
        因为它们不在已核实的字段清单内。
        """
        from hfk.config.keypath import get_nested
        from hfk.config.yaml_io import plain_yaml, read_text
        from hfk.schema import CATALOGUE

        if not self.env.config_readable:
            return {"readable": False, "error": self.env.config_error}
        document = plain_yaml().load(read_text(self.env.config_path)) or {}
        values: dict[str, Any] = {}
        for spec in CATALOGUE:
            exists, value = get_nested(document, spec.path)
            values[spec.path] = {"exists": exists, "value": value if exists else None}
        return {"readable": True, "fields": values}

    # -- 计划 ---------------------------------------------------------------
    def resolve_items(
        self,
        *,
        preset: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把「预设 + 逐项覆盖」解析成最终字段集合。"""
        return compose(preset or DEFAULT_PRESET, overrides or {})

    def preview(
        self,
        *,
        preset: str | None = None,
        overrides: dict[str, Any] | None = None,
        items: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = items if items is not None else self.resolve_items(preset=preset, overrides=overrides)
        plan = patch.build_plan(self.env, resolved, preset=preset or ("custom" if items is not None else DEFAULT_PRESET))
        payload = plan.to_dict()
        payload["explain"] = explain(resolved)
        payload["compat"] = compat_check(self.env.version_tuple, self.env.version).to_dict()
        return payload

    # -- 写入 ---------------------------------------------------------------
    def apply(
        self,
        *,
        preset: str | None = None,
        overrides: dict[str, Any] | None = None,
        items: dict[str, Any] | None = None,
        acknowledge_risk: bool = False,
    ) -> dict[str, Any]:
        self.refresh()
        effective_preset = "custom" if items is not None else (preset or DEFAULT_PRESET)
        meta = get_preset(effective_preset)
        if meta.requires_acknowledgement and not acknowledge_risk:
            raise ConfigValidationError(
                f"预设「{meta.label}」需要显式确认风险后才能应用：{meta.risk_note}"
            )

        resolved = items if items is not None else self.resolve_items(preset=preset, overrides=overrides)
        plan = patch.build_plan(self.env, resolved, preset=effective_preset)
        transaction = patch.apply(self.env, plan)

        # 附加「与当前配置的差异」，供 UI 立刻渲染结果
        report = transaction.to_dict()
        report["plan"] = plan.to_dict()
        report["config_sha256"] = sha256_file(self.env.config_path)
        return report

    def restore(self, transaction_id: str | None = None) -> dict[str, Any]:
        self.refresh()
        report = patch.restore(self.env, transaction_id)
        return report.to_dict()

    def uninstall(self, *, remove_plugin: bool = True) -> dict[str, Any]:
        self.refresh()
        report = patch.uninstall(self.env, remove_plugin=remove_plugin)
        return report.to_dict()

    def diff(
        self,
        *,
        preset: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """快速差异（不做完整校验），供 UI 列表渲染。"""
        document = self.current_config().get("document") or {}
        return diff_against(self.resolve_items(preset=preset, overrides=overrides), document)

    # -- 兼容性 -------------------------------------------------------------
    def compat(self) -> dict[str, Any]:
        return compat_check(self.env.version_tuple, self.env.version).to_dict()
