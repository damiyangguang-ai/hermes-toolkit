"""Freedom Profile：预设、逐项选择与差异预览。

设计要点（对应文档「Freedom Profile」一节）：

* 预设只是**一组字段取值的别名**，不是黑箱。每个预设能说清楚「到底改了哪些键」。
* 用户必须能逐项覆盖预设——所以 :func:`compose` 接受「预设 + 逐项覆盖」。
* 默认预设是 ``balanced``，**不是**「全部关掉」。``freedom`` 必须显式选择并确认风险。
* 任何 B 类字段都不出现在预设里：削弱检测的项目只能由用户逐项打开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hfk.schema import CATALOGUE, DEFAULT_PRESET, FieldSpec, get_spec

__all__ = [
    "Preset",
    "PRESETS",
    "PRESET_ORDER",
    "get_preset",
    "preset_items",
    "compose",
    "explain",
    "diff_against",
]


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    risk_note: str = ""
    requires_acknowledgement: bool = False


PRESET_ORDER: tuple[str, ...] = ("conservative", "balanced", "freedom", "custom")

PRESETS: dict[str, Preset] = {
    "conservative": Preset(
        key="conservative",
        label="保守",
        description=(
            "几乎不改变现有行为：每个危险命令都询问，非交互场景一律拒绝，"
            "破坏性命令仍二次确认。适合先观察 HFK 会改什么。"
        ),
    ),
    "balanced": Preset(
        key="balanced",
        label="平衡（默认）",
        description=(
            "保留智能审批与所有非交互场景的拒绝策略，只去掉明显冗余的二次确认"
            "（/clear、/reload-mcp）并把审批超时收紧到 120 秒。"
        ),
        risk_note="不改变任何「拦不拦」的判断，只改变「问几次、等多久」。",
    ),
    "freedom": Preset(
        key="freedom",
        label="自由",
        description=(
            "审批主模式切到 off（等同 --yolo），并放开 cron / 单次查询 / 无人值守三类场景。"
            "HARDLINE 硬阻断与 approvals.deny 仍然生效。"
        ),
        risk_note=(
            "危险命令不再逐条询问；无人值守平台将完全无人把关。"
            "仅在你能接受 Hermes 自主执行时使用。"
        ),
        requires_acknowledgement=True,
    ),
    "custom": Preset(
        key="custom",
        label="自定义",
        description="不使用任何预设，只应用你在下面逐项勾选的字段。",
    ),
}


def get_preset(name: str | None) -> Preset:
    key = (name or DEFAULT_PRESET).strip().lower()
    if key not in PRESETS:
        raise KeyError(f"未知预设：{name}（可选：{', '.join(PRESET_ORDER)}）")
    return PRESETS[key]


def preset_items(name: str) -> dict[str, Any]:
    """该预设会写入的字段（只包含 A 类；B 类永远不在预设里）。"""
    key = get_preset(name).key
    if key == "custom":
        return {}
    items: dict[str, Any] = {}
    for spec in CATALOGUE:
        if spec.risk != "A":
            continue
        if key in spec.presets:
            items[spec.path] = spec.presets[key]
    return items


def compose(preset_name: str | None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """预设 + 逐项覆盖 → 最终字段集合。

    覆盖值为 ``None`` 表示「这一项不要改」（从预设里剔除）。
    """
    items = preset_items(preset_name or DEFAULT_PRESET)
    for path, value in (overrides or {}).items():
        if value is None:
            items.pop(path, None)
        else:
            items[path] = value
    return items


def explain(items: dict[str, Any]) -> list[dict[str, Any]]:
    """给 UI 的逐项说明：字段、标题、目标值、影响范围、风险级别、证据。"""
    rows: list[dict[str, Any]] = []
    for path, value in items.items():
        spec: FieldSpec | None = get_spec(path)
        rows.append(
            {
                "path": path,
                "title": spec.title if spec else path,
                "value": value,
                "risk": spec.risk if spec else "C",
                "effect": spec.effect if spec else "",
                "surfaces": list(spec.surfaces) if spec else [],
                "evidence": spec.evidence if spec else "",
            }
        )
    return rows


def diff_against(items: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    """与当前配置比较，给出「当前 → 目标」以及是否真的会变化。

    这是 :func:`hfk.engine.patch.build_plan` 的轻量版，供 UI 快速渲染；
    真正执行前仍必须走 build_plan（它还会做父级/取值校验并锁定文件哈希）。
    """
    from hfk.config.keypath import get_nested

    rows: list[dict[str, Any]] = []
    for path, target in items.items():
        exists, current = get_nested(document, path)
        spec = get_spec(path)
        rows.append(
            {
                "path": path,
                "title": spec.title if spec else path,
                "before_exists": exists,
                "before": current,
                "after": target,
                "changed": not (exists and current == target),
                "risk": spec.risk if spec else "C",
                "effect": spec.effect if spec else "",
            }
        )
    return rows
