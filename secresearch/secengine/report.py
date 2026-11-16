"""报告生成：把证据与结论拼成可交付的 Markdown。

两个硬要求来自文档第五模块：
  * 不得把未经验证的猜测写成"已确认漏洞"——``inconclusive`` 必须单列；
  * 必须能给出修复前后的对比。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from .checks import Finding
from .core import Authorization
from .recon import ReconResult

__all__ = ["build_report", "build_scan_report", "count_fixed"]

_VERDICT_LABEL = {
    "vulnerable": "存在",
    "not_vulnerable": "未发现",
    "inconclusive": "无法判定",
}

#: 报告排序用的严重度权重
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _finding_table(findings: list[Finding]) -> list[str]:
    if not findings:
        return ["（无）"]
    lines = [
        _row(["问题", "CWE", "等级", "结论", "证据"]),
        _row(["---"] * 5),
    ]
    for item in findings:
        lines.append(
            _row(
                [
                    item.title,
                    item.cwe,
                    item.severity,
                    _VERDICT_LABEL.get(item.verdict, item.verdict),
                    f"`{item.evidence_id}`" if item.evidence_id else "—",
                ]
            )
        )
    return lines


def count_fixed(before: list[Finding], after: list[Finding]) -> int:
    """修复前存在、修复后不再存在（或被判定未发现）的条数。按 id 匹配，不按下标。"""
    after_map = {item.id: item for item in after}
    total = 0
    for item in before:
        if item.verdict != "vulnerable":
            continue
        later = after_map.get(item.id)
        if later is None or later.verdict != "vulnerable":
            total += 1
    return total


def build_scan_report(run: Any, authorization: Any) -> str:
    """单次授权任务的扫描报告（不做修复前后对比，只交付真实发现）。

    单独成一个函数，是因为"修复前后对比"和"一次扫描的结论"是两种模板；
    把二者混在一起会写出误导性的"已修复"字样。
    """
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append(f"# 授权安全任务报告 · {authorization.target}")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 任务：{run.goal}")
    lines.append(f"- 任务 ID：`{run.task_id}`　状态：**{run.status}**　进度：{run.progress}")
    lines.append(f"- 授权：{authorization.owner}｜证明：{authorization.proof}")
    lines.append(f"- 授权窗口：{authorization.starts_at} → {authorization.ends_at}")
    lines.append(f"- 允许范围：{', '.join(authorization.scopes)}")
    lines.append("")

    lines.append("## 1. 授权范围与执行记录")
    lines.append("")
    lines.append(_row(["#", "步骤", "工具", "尝试", "状态", "结论", "证据"]))
    lines.append(_row(["---"] * 7))
    for step in run.steps:
        lines.append(
            _row(
                [
                    str(step.index),
                    step.kind,
                    step.tool or "—",
                    str(step.attempts),
                    step.status,
                    step.verdict or "—",
                    ", ".join(f"`{item}`" for item in step.evidence_ids) or "—",
                ]
            )
        )
    lines.append("")

    # 侦察结论单独一节，避免与"发现"重复计项
    if getattr(run, "recon_before", None):
        recon = run.recon_before
        server = ((recon.get("headers") or {}).get("Server") or "").strip() or "（无）"
        techs = ", ".join(
            f"{item.get('tech', '')} {item.get('version', '')}".strip()
            for item in (recon.get("tech") or [])
        ) or "（未识别）"
        lines.append("### 侦察结论")
        lines.append("")
        lines.append(f"- HTTP 状态：{recon.get('status')}")
        lines.append(f"- Server：{server}")
        lines.append(f"- 技术栈：{techs}")
        lines.append("")

    # 汇总发现（含响应头类），按严重度排序
    findings = []
    for step in run.steps:
        for item in step.findings:
            findings.append(item)
    findings.sort(key=lambda item: _SEVERITY_ORDER.get(str(item.get("severity")), 9))

    lines.append("## 2. 发现")
    lines.append("")
    if findings:
        lines.append(_row(["问题", "CWE", "等级", "结论", "证据"]))
        lines.append(_row(["---"] * 5))
        for item in findings:
            lines.append(
                _row(
                    [
                        str(item.get("title", item.get("id", ""))),
                        str(item.get("cwe", "—")),
                        str(item.get("severity", "—")),
                        _VERDICT_LABEL.get(str(item.get("verdict")), str(item.get("verdict", "—"))),
                        f"`{item.get('evidence_id')}`" if item.get("evidence_id") else "—",
                    ]
                )
            )
    else:
        lines.append("（本次任务未产生发现）")
    lines.append("")

    lines.append("## 3. 未确认项（不写成漏洞）")
    lines.append("")
    pending = [item for item in findings if item.get("verdict") == "inconclusive"]
    if pending:
        for item in pending:
            lines.append(f"- `{item.get('id')}`：证据不足，标记为无法判定")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 4. 完成校验")
    lines.append("")
    for item in run.verification or ["（尚未校验）"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 5. 限制")
    lines.append("")
    lines.append("- 所有结果来自本机靶场或明确授权目标，未触碰任何未授权资产。")
    lines.append("- 证据中的密码、Token、Cookie 等已脱敏。")
    lines.append("- 未做端口扫描、子域枚举、压力测试；未构造利用链。")
    lines.append("")
    return "\n".join(lines)


def build_report(
    *,
    title: str,
    target: str,
    authorization: Authorization,
    recon_before: ReconResult,
    findings_before: list[Finding],
    recon_after: ReconResult,
    findings_after: list[Finding],
    gate_selfcheck: dict[str, Any],
    evidence_dir: str,
) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 目标：`{target}`")
    lines.append(f"- 授权：{authorization.owner}｜证明：{authorization.proof}")
    lines.append(f"- 授权窗口：{authorization.starts_at} → {authorization.ends_at}")
    lines.append(f"- 允许范围：{', '.join(authorization.scopes)}")
    lines.append(f"- 证据目录：`{evidence_dir}`")
    lines.append("")

    lines.append("## 1. 授权闸门自检")
    lines.append("")
    lines.append(
        f"- 未授权目标尝试主动测试：**{gate_selfcheck['blocked']}**（期望：被拒绝）"
    )
    lines.append(f"- 拒绝原因：{gate_selfcheck['reason']}")
    lines.append("")

    lines.append("## 2. 被动侦察")
    lines.append("")
    lines.append(
        _row(["项目", "值"]),
    )
    lines.append(_row(["---", "---"]))
    lines.append(_row(["HTTP 状态", str(recon_before.status)]))
    lines.append(_row(["Server 头", recon_before.headers.get("Server", "（无）").strip() or "（空）"]))
    tech = ", ".join(
        f"{item['tech']} {item['version']}".strip() for item in recon_before.tech
    ) or "（未识别）"
    lines.append(_row(["技术栈", tech]))
    lines.append("")
    if recon_before.tech:
        lines.append("技术栈线索来源（不是猜测）：")
        for item in recon_before.tech:
            lines.append(f"- `{item['match']}` ← {item['source']}")
        lines.append("")

    lines.append("## 3. 修复前")
    lines.append("")
    lines.extend(_finding_table(findings_before))
    lines.append("")

    lines.append("## 4. 修复后")
    lines.append("")
    lines.extend(_finding_table(findings_after))
    lines.append("")

    lines.append("## 5. 修复前后对比")
    lines.append("")
    before_map = {item.id: item for item in findings_before}
    after_map = {item.id: item for item in findings_after}
    lines.append(_row(["问题", "修复前", "修复后", "结论"]))
    lines.append(_row(["---"] * 4))
    for key in sorted(set(before_map) | set(after_map)):
        before = before_map.get(key)
        after = after_map.get(key)
        b = _VERDICT_LABEL.get(before.verdict, "—") if before else "—"
        if after:
            a = _VERDICT_LABEL.get(after.verdict, "—")
        else:
            # 修复后该项不再出现 = 已经不复现，而不是"没有测"
            a = "未发现（已修复）" if before else "—"
        was_vulnerable = before is not None and before.verdict == "vulnerable"
        still_vulnerable = after is not None and after.verdict == "vulnerable"
        if was_vulnerable and not still_vulnerable:
            verdict = "已修复"
        elif still_vulnerable:
            verdict = "仍存在"
        else:
            verdict = "见上"
        lines.append(_row([key, b, a, verdict]))
    lines.append("")

    pending = [item for item in findings_after if item.verdict == "inconclusive"]
    lines.append("## 6. 未确认项（不写成漏洞）")
    lines.append("")
    if pending:
        for item in pending:
            lines.append(f"- `{item.id}`：{item.title} —— 证据不足，标记为无法判定")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 7. 环境与限制")
    lines.append("")
    lines.append("- 全部测试在**本机回环地址上的自建靶场**执行，未触碰任何第三方目标。")
    lines.append("- 证据中的密码、Token、Cookie 等已脱敏（`[REDACTED]`）。")
    lines.append("- 本报告覆盖已验证的六类问题：SQL 注入、越权访问(IDOR)、反射型 XSS、SSRF、信任客户端角色、安全响应头。未做端口扫描、子域枚举、压力测试。")
    lines.append("")
    return "\n".join(lines)
