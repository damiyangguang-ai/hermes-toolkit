"""Autonomous Task Execution Engine（文档「最高优先级模块」）。

文档的硬要求，逐条对应到实现：

| 文档要求 | 这里的实现 |
|---|---|
| 用户提出任务后自动拆解为可执行步骤 | :meth:`TaskEngine.plan` 按目标与授权范围生成步骤 |
| 自动识别所需工具并检查是否真实可用 | 每一步执行前检查对应检查函数存在且可调用 |
| 自动制定执行计划和完成标准 | ``TaskRun.criteria`` 显式声明"怎样才算完成" |
| 在授权范围内自主执行，不要求逐条下令 | :meth:`TaskEngine.run` 一次跑完，中途不询问 |
| 工具调用、结果读取、分析、失败重试、任务恢复 | 每步记录真实调用；``max_attempts`` 重试；checkpoint 可 resume |
| 每一步保存真实执行记录 | ``ledger`` 落盘，含时间、工具、参数、结果、证据 id |
| 自动检查任务真正完成，而不是凭模型声称 | :meth:`verify` **重新读磁盘上的证据与报告**，不看自述 |
| 超授权/不可逆/高影响操作暂停并请求确认 | 未授权 → 该步 ``blocked``，任务转 ``paused`` |
| 任务进度、取消、暂停、恢复 | 状态机 + checkpoint 文件 + ``resume`` |
| 不允许用模拟执行结果冒充真实执行 | 所有结果均来自真实 HTTP 响应；无响应即 ``failed`` |

**不做的事**：本模块不新建一套 agent loop。它只负责"拆解 → 调用既有检查函数 →
记录 → 校验"，判断与工具都在 :mod:`secengine.checks` 里，文档也要求不要另造框架。
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .checks import (
    Finding,
    check_admin_role,
    check_headers,
    check_idor_profile,
    check_sqli_login,
    check_ssrf_fetch,
    check_xss_search,
)
from .core import AuthorizationError, AuthorizationStore, EvidenceStore
from .recon import run_recon
from .report import build_scan_report, count_fixed

__all__ = ["StepRecord", "TaskRun", "TaskEngine", "STEP_KINDS"]

STEP_KINDS = ("recon", "headers", "sqli", "idor", "xss", "ssrf", "privesc", "report")

#: 需要主动测试（因此需要授权）的步骤类型
_ACTIVE = ("sqli", "idor", "xss", "ssrf", "privesc")

#: 各类检查的默认严重度排序，用于报告里排序
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class StepRecord:
    index: int
    kind: str
    status: str = "pending"      # pending/running/done/failed/blocked/skipped
    attempts: int = 0
    started_at: str = ""
    ended_at: str = ""
    tool: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "status": self.status,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tool": self.tool,
            "params": self.params,
            "verdict": self.verdict,
            "evidence_ids": list(self.evidence_ids),
            "findings": list(self.findings),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            index=int(data["index"]),
            kind=str(data["kind"]),
            status=str(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
            tool=str(data.get("tool", "")),
            params=dict(data.get("params") or {}),
            verdict=str(data.get("verdict", "")),
            evidence_ids=[str(item) for item in data.get("evidence_ids") or []],
            findings=list(data.get("findings") or []),
            error=str(data.get("error", "")),
        )


@dataclass
class TaskRun:
    task_id: str
    goal: str
    target: str
    base_url: str
    steps: list[StepRecord] = field(default_factory=list)
    status: str = "planned"      # planned/running/paused/stopped/completed/failed
    created_at: str = ""
    updated_at: str = ""
    criteria: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    recon_before: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "target": self.target,
            "base_url": self.base_url,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "criteria": list(self.criteria),
            "verification": list(self.verification),
            "steps": [item.to_dict() for item in self.steps],
            "recon_before": self.recon_before,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRun":
        run = cls(
            task_id=str(data["task_id"]),
            goal=str(data.get("goal", "")),
            target=str(data.get("target", "")),
            base_url=str(data.get("base_url", "")),
            status=str(data.get("status", "planned")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            criteria=[str(item) for item in data.get("criteria") or []],
            verification=[str(item) for item in data.get("verification") or []],
            recon_before=data.get("recon_before"),
        )
        run.steps = [StepRecord.from_dict(item) for item in data.get("steps") or []]
        return run

    @property
    def progress(self) -> str:
        done = sum(1 for item in self.steps if item.status in ("done", "skipped"))
        return f"{done}/{len(self.steps)}"


class TaskEngine:
    """把「一个目标」变成「一组真实执行并经验证的步骤」。"""

    def __init__(
        self,
        authz: AuthorizationStore,
        evidence: EvidenceStore,
        out_dir: str | Path,
        *,
        max_attempts: int = 2,
    ) -> None:
        self.authz = authz
        self.evidence = evidence
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir = self.out_dir / "tasks"
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self._stored_findings: list[Finding] = []
        self._last_record: Any = None

    # -- 计划 ---------------------------------------------------------------
    def plan(self, goal: str, target: str, base_url: str) -> TaskRun:
        """自动拆解任务，并写明完成标准。"""
        run = TaskRun(
            task_id="task-" + _dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3),
            goal=goal,
            target=target,
            base_url=base_url,
            created_at=_now(),
            updated_at=_now(),
        )
        kinds: list[str] = ["recon", "headers"]

        # 只有在授权范围内才把主动步骤排进计划——不排无法执行的步骤
        for scope in _ACTIVE:
            if self.authz.find_for(target, scope) is not None:
                kinds.append(scope)
        kinds.append("report")

        run.steps = [StepRecord(index=i, kind=kind) for i, kind in enumerate(kinds)]
        run.criteria = [
            "每个已排入计划的步骤都执行完成（状态 done）",
            "每类主动检查都产生了落盘证据文件",
            "报告文件存在，且包含「授权范围与执行记录」「发现」「未确认项」「完成校验」四部分",
            "报告中出现的 CWE 编号必须来自真实证据，而非模板占位",
            "未授权目标不得产生任何主动测试记录",
        ]
        if not any(item.kind in _ACTIVE for item in run.steps):
            run.criteria.append(
                "注意：该目标未获得任何主动测试授权，本次只做了被动检查（这是设计行为）"
            )
        self._set_status(run, "planned")
        self.save(run)
        return run

    # -- 执行 ---------------------------------------------------------------
    def run(self, run: TaskRun, *, stop_flag: Callable[[], bool] | None = None) -> TaskRun:
        """按计划自主执行。中途不向用户提问；越权即暂停。"""
        self._set_status(run, "running")
        for step in run.steps:
            if step.status in ("done", "skipped"):
                continue
            if stop_flag is not None and stop_flag():
                self._set_status(run, "stopped")
                self.save(run)
                return run
            self._execute(run, step)
            self.save(run)
            if step.status == "blocked":
                # 高影响/未授权：暂停并要求人工确认，绝不"先跑再说"
                self._set_status(run, "paused")
                self.save(run)
                return run
        self._set_status(run, "run_complete")
        self.save(run)
        return run

    def _execute(self, run: TaskRun, step: StepRecord) -> None:
        step.status = "running"
        step.started_at = _now()
        while step.attempts < self.max_attempts:
            step.attempts += 1
            try:
                self._dispatch(run, step)
                step.status = "done"
                step.ended_at = _now()
                return
            except AuthorizationError as exc:
                step.status = "blocked"
                step.error = str(exc)
                step.ended_at = _now()
                return
            except Exception as exc:  # noqa: BLE001
                step.error = f"{type(exc).__name__}: {exc}"
                if step.attempts >= self.max_attempts:
                    step.status = "failed"
                    step.ended_at = _now()
                    return
        step.ended_at = _now()

    def _dispatch(self, run: TaskRun, step: StepRecord) -> None:
        kind = step.kind
        step.params = {"target": run.target, "base_url": run.base_url}

        if kind == "recon":
            step.tool = "run_recon"
            result = run_recon(run.base_url + "/")
            if result.status is None:
                raise RuntimeError(f"侦察失败：{result.error}")
            step.verdict = "ok"
            # 侦察的响应头观察**不**计入"发现"：headers 步骤才是权威结论。
            # 否则报告里同一项会以"结论 —"的形式重复出现（真实踩过）。
            step.findings = []
            step.params = {
                **step.params,
                "server": result.headers.get("Server", "").strip(),
                "tech": [item["tech"] for item in result.tech],
            }
            run.recon_before = result.to_dict()
            return

        if kind == "headers":
            step.tool = "check_headers"
            _, findings = check_headers(run.base_url + "/", evidence_store=self.evidence)
            step.verdict = "vulnerable" if findings else "not_vulnerable"
            step.findings = [item.to_dict() for item in findings]
            step.evidence_ids = sorted({item.evidence_id for item in findings})
            self._stored_findings.extend(findings)
            return

        if kind == "report":
            step.tool = "build_scan_report"
            record = None
            for item in self.authz.load():
                if item.target == run.target.strip().lower():
                    record = item
                    break
            if record is None:
                # 纯被动任务没有主动授权记录，用一条只读占位记录生成报告
                from .core import Authorization  # noqa: PLC0415

                record = Authorization(
                    id="readonly",
                    target=run.target,
                    owner="（只读任务）",
                    proof="（仅被动检查，无主动授权）",
                    starts_at="1970-01-01T00:00:00+00:00",
                    ends_at="9999-12-31T00:00:00+00:00",
                    scopes=[],
                )
            report_path = self.out_dir / "task-report.md"
            self._last_record = record
            report_path.write_text(build_scan_report(run, record) + "\n", encoding="utf-8")
            step.params = {**step.params, "report_path": str(report_path)}
            step.verdict = "written"
            return

        checkers = {
            "sqli": check_sqli_login,
            "idor": check_idor_profile,
            "xss": check_xss_search,
            "ssrf": check_ssrf_fetch,
            "privesc": check_admin_role,
        }
        checker = checkers.get(kind)
        if checker is None:
            # 未知类型必须显式失败，不能静默当成 done
            raise ValueError(f"未知步骤类型：{kind}")

        step.tool = checker.__name__
        _, finding = checker(self.authz, run.target, run.base_url, evidence_store=self.evidence)
        step.verdict = finding.verdict
        step.evidence_ids = [finding.evidence_id]
        step.findings = [finding.to_dict()]
        self._stored_findings.append(finding)

    # -- 校验（文档核心要求：不凭声称，凭磁盘） -------------------------------
    def verify(self, run: TaskRun, report_path: Path | None = None) -> tuple[bool, list[str]]:
        problems: list[str] = []

        unfinished = [item for item in run.steps if item.status not in ("done", "skipped")]
        if unfinished:
            problems.append(
                "未完成的步骤：" + ", ".join(f"#{item.index}:{item.kind}({item.status})" for item in unfinished)
            )

        # 证据文件必须真实存在于磁盘
        on_disk = {path.name.replace(".json", "") for path in self.evidence.directory.glob("ev_*.json")}
        for item in run.steps:
            for evidence_id in item.evidence_ids:
                if evidence_id not in on_disk:
                    problems.append(f"步骤 #{item.index}:{item.kind} 的证据 {evidence_id} 不在磁盘上")

        # 报告必须存在且含四项必需内容（这是**扫描报告**的模板，
        # 不是"修复前后对比"报告的模板——两者的必需章节不同）
        if report_path is not None:
            if not report_path.exists():
                problems.append(f"报告文件不存在：{report_path}")
            else:
                text = report_path.read_text(encoding="utf-8")
                for required in ("授权范围与执行记录", "发现", "未确认项", "完成校验"):
                    if required not in text:
                        problems.append(f"报告缺少必需章节：{required}")
                active = [item for item in run.steps if item.kind in _ACTIVE and item.status == "done"]
                if active and "CWE-" not in text:
                    problems.append("报告没有任何 CWE 编号，但已完成的主动检查应当产生结论")

        run.verification = problems or ["全部校验项通过"]
        ok = not problems
        self._set_status(run, "completed" if ok else "failed")
        self.save(run)

        # 校验结论写回报告，避免交付物里还写着"（尚未校验）"
        if report_path is not None and report_path.exists() and self._last_record is not None:
            report_path.write_text(
                build_scan_report(run, self._last_record) + "\n", encoding="utf-8"
            )
        return ok, run.verification

    # -- 状态与持久化 -------------------------------------------------------
    def _set_status(self, run: TaskRun, status: str) -> None:
        run.status = status
        run.updated_at = _now()

    def save(self, run: TaskRun) -> Path:
        path = self.ledger_dir / f"{run.task_id}.json"
        path.write_text(
            json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def load(self, task_id: str) -> TaskRun:
        path = self.ledger_dir / f"{task_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"找不到任务记录：{path}")
        return TaskRun.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def resume(self, task_id: str, *, stop_flag: Callable[[], bool] | None = None) -> TaskRun:
        """从 checkpoint 恢复：跳过已完成步骤，继续执行剩余步骤。"""
        run = self.load(task_id)
        if run.status == "completed":
            return run
        return self.run(run, stop_flag=stop_flag)
