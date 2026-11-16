"""Expression Engine 第 1–4 条：只读诊断。

对应文档：

1. Roleplay Recognition —— 识别"虚构角色扮演 / 成年亲密关系 / 现实行为"的区别
2. Persona Inspection —— 检查角色卡、SOUL.md、System Prompt 是否存在额外拒绝规则
3. Prompt Conflict Detection —— 检查不同层级提示词是否互相冲突
4. Refusal Diagnostics —— 记录拒绝发生在哪一层，提供证据，不靠表面措辞猜

**本模块只读，不修改任何文件。** 它回答"是哪一层在拒"，不负责让被拒内容通过。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "LayerFinding",
    "DiagnosisReport",
    "scan_text_for_rules",
    "inspect_persona_layers",
    "detect_prompt_conflicts",
    "classify_refusal",
    "diagnose",
]

#: 明确的"内容限制"句式。刻意收紧，避免"性格/属性/major,minor"这类词造成误报。
#: 每一条都是被真实假阳性打出来的：``minor`` 命中过版本号 ``(major, minor)``，
#: ``consent`` 命中过工具调用同意，``性`` 命中过"属性"。
_RULE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"不得(生成|输出|描写|讨论|提供)", "high", "不得生成类表述"),
    (r"禁止(生成|输出|描写|讨论|提供|涉及)", "high", "禁止生成类表述"),
    (r"(不要|不能|不可)(生成|输出|描写)", "medium", "不要生成类表述"),
    (r"拒绝(生成|回答|讨论|提供)", "high", "拒绝类表述"),
    (r"(成人内容|色情内容|露骨描写|情色场景)", "high", "成人内容专指"),
    (r"(nsfw|explicit sexual|sexual content|adult content)", "high", "英文成人内容专指"),
    (r"(未成年|未满\d+岁|underage)", "high", "未成年人保护条款"),
    (r"(非自愿|non-consensual)", "high", "同意条款"),
    (r"(内容政策|内容审核|content policy|safety policy)", "high", "内容政策引用"),
)

#: 出现这些组合才算"内容限制"，单独一个词不算
_STRONG_COMBOS = (
    (r"拒绝", r"(色情|成人|性行为|NSFW)"),
    (r"禁止", r"(色情|成人|性行为|NSFW)"),
)

#: 出现这些组合才算"内容限制"，单独一个词不算
_STRONG_COMBOS = (
    (r"拒绝", r"(色情|成人|性|NSFW)"),
    (r"禁止", r"(色情|成人|性|NSFW)"),
)


@dataclass
class LayerFinding:
    layer: str                 # soul / system_prompt / persona_card / profile / plugin
    path: str
    line: int
    text: str
    kind: str                  # restriction / persona / conflict / neutral
    confidence: str            # high / medium / low
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "kind": self.kind,
            "confidence": self.confidence,
            "note": self.note,
        }


def scan_text_for_rules(text: str, *, layer: str, path: str) -> list[LayerFinding]:
    """扫描文本里的内容限制句式。每一条都带行号，便于人工复核。"""
    findings: list[LayerFinding] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        for pattern, confidence, label in _RULE_PATTERNS:
            if re.search(pattern, line, re.I):
                findings.append(
                    LayerFinding(
                        layer=layer,
                        path=path,
                        line=index,
                        text=line[:240],
                        kind="restriction",
                        confidence=confidence,
                        note=label,
                    )
                )
                break
        else:
            # 弱信号：必须同时命中"拒绝/禁止 + 成人相关词"才算
            for left, right in _STRONG_COMBOS:
                if re.search(left, line, re.I) and re.search(right, line, re.I):
                    findings.append(
                        LayerFinding(
                            layer=layer,
                            path=path,
                            line=index,
                            text=line[:240],
                            kind="restriction",
                            confidence="medium",
                            note="拒绝+成人词组合",
                        )
                    )
                    break
    return findings


#: 候选的系统提示词文件（相对 Hermes 安装目录）。找到几个算几个，不猜存在。
_PROMPT_CANDIDATES = (
    "prompts/system_prompt.md",
    "prompts/system.md",
    "prompts/base.md",
    "references/system-prompt-invariant.md",
    "hermes_cli/prompts/system_prompt.txt",
    "agent/prompt_builder.py",
    "agent/system_prompt.py",
    "hermes_cli/system_prompt.py",
    "core/system_prompt.py",
)

#: 动态发现：这些目录下的 .py/.md/.txt 若含提示词特征词，就当作候选提示层
_PROMPT_DIRS = ("agent", "hermes_cli", "references", "prompts", "core")

#: 判断"这个文件承载提示词文本"的特征词
_PROMPT_MARKERS = re.compile(
    r"(You are |You must|You should|你是|你必须|system prompt|GUIDANCE|DEFAULT_AGENT_IDENTITY)",
    re.I,
)

#: .py 文件里嵌入的提示词常量通常是长三引号字符串，低于这个长度不算
_PROMPT_STRING_MIN = 400


def _looks_like_prompt_container(path: Path, text: str) -> bool:
    """判断文件是否**真的承载提示词文本**，而不是普通代码。

    只看"含 GUIDANCE 字样"会把 ``auxiliary_client.py``（440KB 业务代码）也当成
    提示层——第一版就这么干了，导致层清单里塞满无关文件、限制命中全是假阳性。
    第二版只看"有没有 400 字以上的三引号字符串"，仍然把长 docstring 收进来。

    现在的要求：**长字符串内部**必须出现提示词特征词（You are / 你是 / GUIDANCE …）。
    普通模块 docstring 不会通过这一关。
    """
    if path.suffix.lower() in (".md", ".txt"):
        return len(text) > 1500
    if path.suffix.lower() != ".py":
        return False
    for match in re.finditer(r'("""|\'\'\')([\s\S]{0,200000}?)\1', text):
        body = match.group(2)
        if len(body) >= _PROMPT_STRING_MIN and _PROMPT_MARKERS.search(body):
            return True
    return False

#: 角色卡 / 人格文件候选（用户可写层）
_PERSONA_CANDIDATES = ("SOUL.md", "PERSONA.md", "persona.md", "AGENTS.md", "USER.md", "MEMORY.md")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def discover_prompt_files(install_dir: Path, *, limit: int = 12) -> list[Path]:
    """动态发现承载提示词文本的文件。

    为什么不能只靠固定候选清单：``agent/system_prompt.py`` 只是**拼装器**，
    真正的提示词常量在 ``agent/prompt_builder.py``、``references/*.md`` 里。
    只扫拼装器会得到"0 条限制"的**假阴性**——这是实际踩过的坑。
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path.is_file() and path not in seen:
            seen.add(path)
            found.append(path)

    for relative in _PROMPT_CANDIDATES:
        path = install_dir / relative
        if path.is_file():
            text = _read(path)
            if _looks_like_prompt_container(path, text):
                add(path)

    for dirname in _PROMPT_DIRS:
        base = install_dir / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if len(found) >= limit:
                return found
            if not path.is_file() or path.suffix.lower() not in (".py", ".md", ".txt"):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < 1500 or size > 2_000_000:
                continue
            text = _read(path)
            if _looks_like_prompt_container(path, text):
                add(path)
    return found


def inspect_persona_layers(home: Path, install_dir: Path | None = None) -> dict[str, Any]:
    """逐层检查：SOUL.md / 角色卡 / 系统提示词 / profile / 插件。

    ``install_dir`` 为 None 时只检查用户可写层（SOUL、角色卡、插件），
    并明确报告"系统提示词未检查"而不是假装检查过。
    """
    layers: list[dict[str, Any]] = []
    findings: list[LayerFinding] = []

    for name in _PERSONA_CANDIDATES:
        path = home / name
        if not path.exists():
            continue
        text = _read(path)
        rules = scan_text_for_rules(text, layer="soul", path=str(path))
        findings.extend(rules)
        layers.append(
            {
                "layer": "soul",
                "path": str(path),
                "exists": True,
                "bytes": len(text.encode("utf-8")),
                "lines": len(text.splitlines()),
                "restriction_hits": len(rules),
            }
        )

    if not any(item["layer"] == "soul" for item in layers):
        layers.append({"layer": "soul", "path": str(home), "exists": False, "restriction_hits": 0})

    if install_dir is not None and install_dir.exists():
        prompt_files = discover_prompt_files(install_dir)
        for path in prompt_files:
            text = _read(path)
            rules = scan_text_for_rules(text, layer="system_prompt", path=str(path))
            findings.extend(rules)
            layers.append(
                {
                    "layer": "system_prompt",
                    "path": str(path),
                    "exists": True,
                    "bytes": len(text.encode("utf-8")),
                    "lines": len(text.splitlines()),
                    "restriction_hits": len(rules),
                }
            )
        if not prompt_files:
            layers.append(
                {
                    "layer": "system_prompt",
                    "path": str(install_dir),
                    "exists": False,
                    "restriction_hits": 0,
                    "note": "未发现承载提示词的文件（不等于不存在，需进一步定位）",
                }
            )

    # 插件层：检查有没有插件往提示词里注入内容
    plugins_dir = home / "desktop-plugins"
    if plugins_dir.is_dir():
        for plugin in sorted(plugins_dir.glob("*/plugin.js")):
            text = _read(plugin)
            injected = bool(re.search(r"(systemPrompt|system_prompt|messages\.unshift|appendSystem)", text))
            findings.extend(
                scan_text_for_rules(text, layer="plugin", path=str(plugin))
            )
            layers.append(
                {
                    "layer": "plugin",
                    "path": str(plugin),
                    "exists": True,
                    "bytes": len(text.encode("utf-8")),
                    "lines": len(text.splitlines()),
                    "prompt_injection_capable": injected,
                    "restriction_hits": len(
                        scan_text_for_rules(text, layer="plugin", path=str(plugin))
                    ),
                }
            )

    conflicts = detect_prompt_conflicts(findings)
    return {"layers": layers, "findings": [item.to_dict() for item in findings], "conflicts": conflicts}


def detect_prompt_conflicts(findings: Iterable[LayerFinding]) -> list[dict[str, Any]]:
    """检测跨层指令冲突。

    明确的判据：同一"主题词"在不同层里，一层说"必须/允许"，另一层说"禁止/不得"。
    这是**候选**冲突，需要人工确认——所以字段名叫 ``candidates``，不叫"已确认冲突"。
    """
    topics = {
        "成人内容": r"(成人|色情|露骨|情色|nsfw|sexual)",
        "角色扮演": r"(角色扮演|roleplay|扮演|角色卡)",
        "亲密关系": r"(亲密|恋爱|暧昧|调情|romance|intimacy)",
        "真实性": r"(真实人物|真人|real person|公众人物)",
    }
    allow = re.compile(r"(允许|可以|支持|应当|必须)(生成|输出|描写|讨论)?")
    deny = re.compile(r"(禁止|不得|不可|不能|拒绝|不要)(生成|输出|描写|讨论)?")

    candidates: list[dict[str, Any]] = []
    for topic, topic_re in topics.items():
        allow_hits = [
            item
            for item in findings
            if re.search(topic_re, item.text, re.I) and allow.search(item.text)
        ]
        deny_hits = [
            item
            for item in findings
            if re.search(topic_re, item.text, re.I) and deny.search(item.text)
        ]
        if allow_hits and deny_hits:
            layers_a = {item.layer for item in allow_hits}
            layers_d = {item.layer for item in deny_hits}
            if layers_a != layers_d or len(deny_hits) > 1:
                candidates.append(
                    {
                        "topic": topic,
                        "allow": [f"{i.layer}:{i.line}" for i in allow_hits],
                        "deny": [f"{i.layer}:{i.line}" for i in deny_hits],
                        "note": "候选冲突：同一主题在不同层出现相反指令，需人工确认",
                    }
                )
    return candidates


# --------------------------------------------------------------------------- 拒绝归因

#: 供应商侧内容策略错误码特征 —— 出现即证明"拒绝来自 provider"
_PROVIDER_MARKERS = (
    "content_filter",
    "content_policy",
    "moderation_blocked",
    "guardrail",
    "data_inspection",
    "safety_filter",
    "safety system",
    "request was rejected",
)

#: 本地拦截特征 —— 出现即证明"请求没出去"
_LOCAL_MARKERS = (
    "blocked_by_hardline",
    "HARDLINE",
    "command_denied",
    "approval_required",
    "denied by policy",
)


def classify_refusal(evidence_text: str, *, request_sent: bool | None = None) -> dict[str, Any]:
    """判断一次拒绝发生在哪一层。

    判据优先级：
      1. 出现供应商策略错误码 → provider
      2. 出现本地硬阻断标记 → local
      3. ``request_sent=True`` 但没有任何错误码 → model_judgment（模型自己拒）
      4. 其余 → unknown（**不猜**）
    """
    text = evidence_text or ""
    lowered = text.lower()

    provider_hits = [item for item in _PROVIDER_MARKERS if item in lowered]
    local_hits = [item for item in _LOCAL_MARKERS if item in lowered]

    if provider_hits:
        return {
            "layer": "provider",
            "confidence": "high",
            "evidence": provider_hits,
            "reason": "响应中出现供应商内容策略错误码，说明请求到达了 provider 并被其拦截",
        }
    if local_hits:
        return {
            "layer": "local",
            "confidence": "high",
            "evidence": local_hits,
            "reason": "出现本地拦截标记，说明请求未发出或被本地守卫拦下",
        }
    if request_sent is True:
        return {
            "layer": "model_judgment",
            "confidence": "medium",
            "evidence": [],
            "reason": "请求已发出、响应无供应商策略错误码，判断为模型基于自身判断的拒绝",
        }
    return {
        "layer": "unknown",
        "confidence": "low",
        "evidence": [],
        "reason": "缺少可靠证据（既无策略错误码，也无法确认请求是否发出）——标记为 Unknown，不猜测",
    }


# --------------------------------------------------------------------------- 汇总


@dataclass
class DiagnosisReport:
    home: str
    install_dir: str
    layers: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    conclusion: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "install_dir": self.install_dir,
            "layers": self.layers,
            "findings": self.findings,
            "conflicts": self.conflicts,
            "conclusion": self.conclusion,
            "unknowns": self.unknowns,
        }


def diagnose(home: Path, install_dir: Path | None = None) -> DiagnosisReport:
    report = DiagnosisReport(home=str(home), install_dir=str(install_dir or ""))
    inspected = inspect_persona_layers(home, install_dir)
    report.layers = inspected["layers"]
    report.findings = inspected["findings"]
    report.conflicts = inspected["conflicts"]

    restrict = [item for item in report.findings if item.get("kind") == "restriction"]
    report.conclusion.append(f"共检查 {len(report.layers)} 个提示层")
    if restrict:
        report.conclusion.append(
            f"发现 {len(restrict)} 条内容限制句式 → 这些是**本地层**的限制，可被人为修改"
        )
        for item in restrict[:10]:
            report.conclusion.append(
                f"  · {item['layer']} {item['path']}:{item['line']} [{item['confidence']}] {item['text'][:80]}"
            )
    else:
        report.conclusion.append(
            "未在任何本地提示层发现内容限制句式 → 本地层不是拒绝来源"
        )

    if report.conflicts:
        report.conclusion.append(f"发现 {len(report.conflicts)} 处候选提示词冲突（需人工确认）")

    report.unknowns.append(
        "provider 侧的请求/响应正文未在本工具读取范围内；拒绝归因需用 classify_refusal() 对真实错误码判定"
    )
    prompt_layers = [item for item in report.layers if item.get("layer") == "system_prompt"]
    report.unknowns.append(
        f"提示词文件为**启发式发现**（扫描 {len(prompt_layers)} 个文件），"
        "「未发现」只代表已扫文件中没有，不等于全局不存在"
    )
    if not app_path_exists(install_dir):
        report.unknowns.append("未提供 Hermes 安装目录，系统提示词层未检查")
    return report


def app_path_exists(path: Path | None) -> bool:
    return path is not None and path.exists()


# --------------------------------------------------------------------------- 报告渲染


def render_markdown(report: DiagnosisReport) -> str:
    lines = ["# Expression Diagnostics · 只读诊断报告", ""]
    lines.append(f"- Hermes Home：`{report.home}`")
    lines.append(f"- 安装目录：`{report.install_dir or '（未提供）'}`")
    lines.append("")

    lines.append("## 1. 提示层清单")
    lines.append("")
    lines.append("| 层 | 路径 | 存在 | 字节 | 行数 | 限制命中 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for layer in report.layers:
        lines.append(
            f"| {layer.get('layer')} | `{layer.get('path')}` | {layer.get('exists')} | "
            f"{layer.get('bytes', '—')} | {layer.get('lines', '—')} | {layer.get('restriction_hits', 0)} |"
        )
    lines.append("")

    lines.append("## 2. 内容限制句式（角色扮演识别 / 人设检查）")
    lines.append("")
    restrict = [item for item in report.findings if item.get("kind") == "restriction"]
    if restrict:
        lines.append("| 层 | 位置 | 置信度 | 原文 |")
        lines.append("| --- | --- | --- | --- |")
        for item in restrict:
            lines.append(
                f"| {item['layer']} | `{Path(item['path']).name}:{item['line']}` | "
                f"{item['confidence']} | {str(item['text'])[:100]} |"
            )
    else:
        lines.append("未发现内容限制句式。")
    lines.append("")

    lines.append("## 3. 提示词冲突（候选）")
    lines.append("")
    if report.conflicts:
        for item in report.conflicts:
            lines.append(f"- **{item['topic']}**：允许 {item['allow']} / 拒绝 {item['deny']}")
    else:
        lines.append("未发现跨层相反指令。")
    lines.append("")

    lines.append("## 4. 结论")
    lines.append("")
    for item in report.conclusion:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 5. 未确认项（标记 Unknown，不猜测）")
    lines.append("")
    for item in report.unknowns:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 6. 边界声明")
    lines.append("")
    lines.append("- 本报告**只读**，未修改任何文件。")
    lines.append("- 它回答「哪一层在拒」，**不负责让被拒内容通过**。")
    lines.append("- 本地层若确有限制可以人为修改；provider 侧策略不在本地可控范围内。")
    lines.append("")
    return "\n".join(lines)


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
