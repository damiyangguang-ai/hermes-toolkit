"""已验证配置字段清单（HFK 的安全核心）。

**为什么需要这一层**：文档明确要求「每个配置项都必须先从目标版本源码、Schema、默认配置中
验证是否真实存在」「不能凭字段名称猜测配置」「不存在的字段不能写入 config.yaml」。

本清单的每一条都来自 Hermes v0.23.1 的 ``hermes_cli/config_defaults.py``
``DEFAULT_CONFIG["approvals"]``（约 1646–1682 行）与相邻顶层键，并经
``scripts/probe_schema.py`` 在真实运行时导出核对。没有证据的字段一律不收录。

**风险分级**（对应文档要求的 A/B/C 三分，绝不允许混为一谈）：
  * ``A`` 普通、可恢复：只是改变「要不要问一次」，任何时刻可逐字段还原。
  * ``B`` 高风险但可由用户管理：会削弱某类检测/扩大无人值守执行面，
        必须逐项显式选择、默认不选、并在界面标注后果。
  * ``C`` 不可恢复：HFK **永不写入**，只在界面里说明「为什么不能改」。

注意：本模块不读取、不保存任何 API Key / Token / 密码字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Risk = Literal["A", "B", "C"]

__all__ = [
    "FieldSpec",
    "CATALOGUE",
    "BLOCKED_FIELDS",
    "DEFAULT_PRESET",
    "get_spec",
    "spec_paths",
    "specs_by_risk",
    "validate_value",
    "describe_values",
]


@dataclass(frozen=True)
class FieldSpec:
    """一个「已核实」的配置字段。"""

    path: str
    title: str
    kind: Literal["enum", "int", "bool", "str", "list"]
    default: Any = None
    values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    risk: Risk = "A"
    presets: dict[str, Any] = field(default_factory=dict)
    effect: str = ""
    surfaces: tuple[str, ...] = ()
    evidence: str = ""
    notes: str = ""
    unit_seconds: bool = False

    def describe_type(self) -> str:
        if self.kind == "enum":
            return " / ".join(self.values)
        if self.kind == "int":
            low = "−∞" if self.minimum is None else str(self.minimum)
            high = "+∞" if self.maximum is None else str(self.maximum)
            return f"整数 {low}…{high}（秒）" if self.unit_seconds else f"整数 {low}…{high}"
        if self.kind == "bool":
            return "true / false"
        if self.kind == "list":
            return "字符串列表"
        return "字符串"


#: 默认预设：**不是**「全部关掉」，而是一个仍然保留提示、但减少无意义重复确认的档位。
DEFAULT_PRESET = "balanced"


_EVIDENCE_APPROVALS = "hermes_cli/config_defaults.py:1646-1680 (DEFAULT_CONFIG['approvals'])"
_EVIDENCE_ALLOWLIST = "hermes_cli/config_defaults.py:1681-1682 (DEFAULT_CONFIG['command_allowlist'])"

CATALOGUE: tuple[FieldSpec, ...] = (
    # ---------------------------------------------------------------- 审批主档
    FieldSpec(
        path="approvals.mode",
        title="审批主模式",
        kind="enum",
        values=("manual", "smart", "off"),
        default="smart",
        risk="A",
        presets={"conservative": "manual", "balanced": "smart", "freedom": "off"},
        effect=(
            "manual＝每个危险命令都问你；smart＝由辅助模型判定低风险项自动放行；"
            "off＝等同于 --yolo，不再逐条询问。"
        ),
        surfaces=("CLI", "Gateway", "Desktop", "Cron", "Unattended"),
        evidence=_EVIDENCE_APPROVALS + " 注释 'mode: manual (always prompt) | smart (aux LLM auto-approves low-risk) | off (= --yolo)'",
        notes="即使 off，HARDLINE 硬阻断与 approvals.deny 仍然生效，不会被本项解除。",
    ),
    FieldSpec(
        path="approvals.timeout",
        title="审批等待超时",
        kind="int",
        default=300,
        minimum=1,
        maximum=3600,
        risk="A",
        presets={"conservative": 300, "balanced": 120, "freedom": 60},
        effect="审批提示最多等多少秒；超时按 fail-closed 处理（视为拒绝）。",
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence=_EVIDENCE_APPROVALS + " 注释 'timeout: seconds before an unanswered prompt fails closed'",
        unit_seconds=True,
    ),
    # ---------------------------------------------------------------- 非交互场景
    FieldSpec(
        path="approvals.cron_mode",
        title="定时任务审批策略",
        kind="enum",
        values=("deny", "approve"),
        default="deny",
        risk="A",
        presets={"conservative": "deny", "balanced": "deny", "freedom": "approve"},
        effect="Cron 任务遇到危险命令时：deny＝立刻拦下让 Agent 换方案；approve＝自动放行。",
        surfaces=("Cron",),
        evidence=_EVIDENCE_APPROVALS + " 'cron_mode': 'deny'",
        notes="deny 的价值是「立刻失败」而不是「等满超时再失败」，避免无人值守任务卡死。",
    ),
    FieldSpec(
        path="approvals.single_query_mode",
        title="单次查询审批策略",
        kind="enum",
        values=("deny", "approve"),
        default="deny",
        risk="A",
        presets={"conservative": "deny", "balanced": "deny", "freedom": "approve"},
        effect="`-q`/`-Q` 一次性会话没有人在等审批：deny＝直接拦下，approve＝自动放行。",
        surfaces=("Single Query",),
        evidence=_EVIDENCE_APPROVALS + " 'single_query_mode': 'deny'",
    ),
    FieldSpec(
        path="approvals.unattended_mode",
        title="无人值守平台审批策略",
        kind="enum",
        values=("deny", "approve"),
        default="deny",
        risk="A",
        presets={"conservative": "deny", "balanced": "deny", "freedom": "approve"},
        effect="webhook / api_server 等没有 /approve 通道的平台：deny＝直接拦下，approve＝自动放行。",
        surfaces=("Unattended",),
        evidence=_EVIDENCE_APPROVALS + " 'unattended_mode': 'deny'",
        notes="这类平台根本没有「回答审批」的通道，approve 等于完全无人把关。",
    ),
    # ---------------------------------------------------------------- 交互体验
    FieldSpec(
        path="approvals.destructive_slash_confirm",
        title="破坏性斜杠命令二次确认",
        kind="bool",
        default=True,
        risk="A",
        presets={"conservative": True, "balanced": False, "freedom": False},
        effect="/clear /new /reset /undo 这类会丢弃会话状态的命令，是否先弹一次确认。",
        surfaces=("Desktop", "CLI", "Gateway"),
        evidence=_EVIDENCE_APPROVALS + " 'destructive_slash_confirm': True（gateway/run_busy.py 读取）",
    ),
    FieldSpec(
        path="approvals.mcp_reload_confirm",
        title="MCP 重载确认",
        kind="bool",
        default=True,
        risk="A",
        presets={"conservative": True, "balanced": False, "freedom": False},
        effect="/reload-mcp 重建 MCP 工具集前是否确认（重载会让 prompt 缓存失效）。",
        surfaces=("Desktop", "CLI", "Gateway"),
        evidence=_EVIDENCE_APPROVALS + " 'mcp_reload_confirm': True",
    ),
    FieldSpec(
        path="approvals.discord_prompt_timeout",
        title="Discord 审批按钮超时",
        kind="int",
        default=300,
        minimum=1,
        maximum=3600,
        risk="A",
        presets={},
        effect="Discord 审批按钮可点击的时长（最短 30 秒，上限受 MAX_SAFE_TIMEOUT_S 约束）。",
        surfaces=("Gateway",),
        evidence=_EVIDENCE_APPROVALS + " 'discord_prompt_timeout': 300",
        unit_seconds=True,
    ),
    FieldSpec(
        path="approvals.denial_breaker_threshold",
        title="连续拒绝熔断阈值",
        kind="int",
        default=3,
        minimum=0,
        maximum=50,
        risk="A",
        presets={"conservative": 3, "balanced": 5, "freedom": 0},
        effect="同一会话里辅助模型连续拒绝多少次后升级为硬停止；0 表示关闭该熔断。",
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence=_EVIDENCE_APPROVALS + " 'denial_breaker_threshold': 3",
    ),
    # ---------------------------------------------------------------- B 类：削弱检测
    FieldSpec(
        path="command_allowlist",
        title="永久放行的危险命令类别",
        kind="list",
        default=[],
        risk="B",
        presets={},
        effect=(
            "列表里的「类别名」一旦命中，该类别从此不再询问——相当于永久跳过对应检测。"
            "这是真正会削弱防护的一项，默认永远为空，且永不进入任何预设。"
        ),
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence=_EVIDENCE_ALLOWLIST + " 注释 'Permanently allowed dangerous command patterns (added via \"always\" approval)'",
        notes="实测本机该项存的是类别标签（如 'recursive delete'），不是命令原文。",
    ),
    FieldSpec(
        path="approvals.smart_policy",
        title="智能审批附加策略",
        kind="str",
        default="",
        risk="B",
        presets={},
        effect=(
            "追加到智能审批「守卫模型」系统提示末尾的额外规则，例如“涉及 /etc 一律升级”。"
            "写反了会削弱守卫本身的判断。"
        ),
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence=_EVIDENCE_APPROVALS + " 'smart_policy': ''",
    ),
    FieldSpec(
        path="approvals.deny",
        title="强制阻断规则（fnmatch）",
        kind="list",
        default=[],
        risk="B",
        presets={},
        effect=(
            "命令行 glob 黑名单：命中即拦截，**即使 --yolo 或 mode=off 也照样拦**。"
            "这是你自己的最后一道闸；删除条目等于削弱它。"
        ),
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence=_EVIDENCE_APPROVALS + " 'deny': []，注释 'a match blocks even under --yolo / mode=off'",
        notes="HFK 只允许新增条目来加强，删除需逐条显式确认。",
    ),
    FieldSpec(
        path="approvals.approvers",
        title="谁可以回答审批",
        kind="list",
        default=["*"],
        risk="B",
        presets={},
        effect='["*"] ＝任何已入群成员都能批准；收窄为具体 ID 可减少被他人代批的风险。',
        surfaces=("Gateway",),
        evidence=_EVIDENCE_APPROVALS + " 'approvers': ['*']",
    ),
    FieldSpec(
        path="delegation.subagent_auto_approve",
        title="子代理自动批准",
        kind="bool",
        default=False,
        risk="B",
        presets={},
        effect="子代理执行工具调用时是否免审批——会显著扩大无人把关的执行面。",
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence="hermes_cli/config_defaults.py (delegation.subagent_auto_approve, default False)",
    ),
    FieldSpec(
        path="hooks_auto_accept",
        title="自动接受 Shell Hook 注册",
        kind="bool",
        default=False,
        risk="B",
        presets={},
        effect="是否免 TTY 提示直接接受新注册的 shell hook；hook 会执行真实命令。",
        surfaces=("CLI", "Gateway", "Cron"),
        evidence="hermes_cli/config_defaults.py:1729 'hooks_auto_accept': False",
    ),
    FieldSpec(
        path="memory.write_approval",
        title="记忆写入需审批",
        kind="bool",
        default=False,
        risk="A",
        presets={},
        effect="Agent 写入长期记忆前是否需要你确认。",
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence="hermes_cli/config_defaults.py (memory.write_approval, default False)",
    ),
    FieldSpec(
        path="skills.write_approval",
        title="技能写入需审批",
        kind="bool",
        default=False,
        risk="A",
        presets={},
        effect="Agent 写入/修改技能文件前是否需要你确认。",
        surfaces=("CLI", "Gateway", "Desktop"),
        evidence="hermes_cli/config_defaults.py (skills.write_approval, default False)",
    ),
)

#: C 类：HFK **永不写入**，只做解释。它们不是「配置项」，而是安全底线本身。
BLOCKED_FIELDS: dict[str, str] = {
    "security.approval.transport": (
        "审批传输通道属于企业级安全边界（builtin/外部通道），改错会让审批整体失效且难以察觉，"
        "HFK 0.1.0 不提供修改。"
    ),
    "auxiliary.approval.api_key": (
        "这是辅助审批模型的 API Key，属于秘密。HFK 不读取、不写入、不记录任何秘密。"
    ),
    "auxiliary.approval.provider": (
        "更换辅助审批模型提供商会改变「谁来判定低风险」，涉及外部服务与凭据，超出 0.1.0 范围。"
    ),
}

#: 代码级安全底线：不存在配置开关，因此 HFK 在架构上就无法削弱它们。
CODE_LEVEL_FLOORS: tuple[tuple[str, str], ...] = (
    ("HARDLINE_PATTERNS", "tools/approval_floors.py —— 不可恢复系统破坏的硬阻断，任何模式都不放行。"),
    ("凭据检测 / 凭据上传检测", "tools/approval_detection.py —— 防止把密钥、Token 读出或外传。"),
    ("Runtime Self-Delete Protection", "tools/approval_detection.py —— 防止 Agent 删除自身运行时。"),
    ("Sudo Password Stdin Guard", "tools/approval_detection.py —— 防止通过 stdin 喂密码。"),
    ("审批上下文隔离", "tools/approval_context.py —— session/turn/tool_call 隔离与 _YOLO_MODE_FROZEN。"),
)

_BY_PATH = {spec.path: spec for spec in CATALOGUE}


def get_spec(path: str) -> FieldSpec | None:
    return _BY_PATH.get(path)


def spec_paths() -> tuple[str, ...]:
    return tuple(_BY_PATH)


def specs_by_risk(risk: Risk) -> tuple[FieldSpec, ...]:
    return tuple(spec for spec in CATALOGUE if spec.risk == risk)


def describe_values(spec: FieldSpec) -> tuple[str, ...]:
    """给 UI 用的可选值文案。"""
    if spec.kind == "enum":
        return spec.values
    if spec.kind == "bool":
        return ("true", "false")
    return ()


def validate_value(path: str, value: Any) -> list[str]:
    """校验一个待写入值，返回错误文案列表（空列表＝通过）。

    这里只做「值本身」的校验；「字段是否存在 / 版本是否支持」由兼容层负责。
    """
    spec = get_spec(path)
    if spec is None:
        if path in BLOCKED_FIELDS:
            return [f"{path} 被 HFK 明确排除：{BLOCKED_FIELDS[path]}"]
        return [f"{path} 不在已验证字段清单内，拒绝写入"]

    errors: list[str] = []
    if spec.kind == "enum":
        if not isinstance(value, str) or value not in spec.values:
            errors.append(f"{path} 只接受 {list(spec.values)}，收到 {value!r}")
    elif spec.kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path} 只接受布尔值，收到 {type(value).__name__}")
    elif spec.kind == "int":
        # bool 是 int 的子类，必须单独排除
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path} 只接受整数，收到 {type(value).__name__}")
        else:
            if spec.minimum is not None and value < spec.minimum:
                errors.append(f"{path} 不得小于 {spec.minimum}，收到 {value}")
            if spec.maximum is not None and value > spec.maximum:
                errors.append(f"{path} 不得大于 {spec.maximum}，收到 {value}")
    elif spec.kind == "str":
        if not isinstance(value, str):
            errors.append(f"{path} 只接受字符串，收到 {type(value).__name__}")
    elif spec.kind == "list":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{path} 只接受字符串列表")
    return errors
