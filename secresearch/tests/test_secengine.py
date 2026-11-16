"""SecResearch 单元测试。

覆盖三类"如果坏了会很危险"的行为：
  1. 授权闸门（没有授权绝不跑主动测试）；
  2. 检测判据（真漏洞必须检出，修复后必须消失）；
  3. 脱敏与报告（凭据不能落盘、inconclusive 不能写成漏洞）。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from secengine.checks import (
    check_admin_role,
    check_headers,
    check_idor_profile,
    check_sqli_login,
    check_ssrf_fetch,
    check_xss_search,
)
from secengine.core import (
    Authorization,
    AuthorizationError,
    AuthorizationStore,
    EvidenceStore,
    redact,
)
from secengine.report import build_report, count_fixed
from secengine.recon import analyze_headers, fingerprint
from secengine.checks import Finding

from lab.vulnapp import create_server


def _iso(hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def make_record(target: str, *, scopes=("sqli", "idor"), start=-1, end=2) -> Authorization:
    return Authorization(
        id="authz-test",
        target=target,
        owner="测试所有者",
        proof="自建靶场",
        starts_at=_iso(start),
        ends_at=_iso(end),
        scopes=list(scopes),
    )


class LabMixin(unittest.TestCase):
    fixed = False

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="secengine-test-"))
        self.server, self.port, _ = create_server(0, fixed=self.fixed)
        self.target = f"127.0.0.1:{self.port}"
        self.base = f"http://{self.target}"
        self.store = AuthorizationStore(self._tmp / "authz.json")
        self.evidence = EvidenceStore(self._tmp / "evidence")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestAuthorizationGate(LabMixin):
    """默认拒绝：这是整个模块的安全底线。"""

    def test_refuses_without_any_record(self):
        with self.assertRaises(AuthorizationError) as ctx:
            check_sqli_login(self.store, self.target, self.base)
        self.assertIn("缺少有效授权", str(ctx.exception))

    def test_refuses_expired_record(self):
        self.store.add(make_record(self.target, start=-10, end=-5))
        with self.assertRaises(AuthorizationError):
            check_sqli_login(self.store, self.target, self.base)

    def test_refuses_wrong_scope(self):
        self.store.add(make_record(self.target, scopes=("xss",)))
        with self.assertRaises(AuthorizationError):
            check_sqli_login(self.store, self.target, self.base)

    def test_refuses_different_target(self):
        self.store.add(make_record(self.target))
        with self.assertRaises(AuthorizationError):
            check_sqli_login(self.store, "example.com:443", "http://example.com")

    def test_reason_mentions_target_mismatch(self):
        self.store.add(make_record(self.target))
        with self.assertRaises(AuthorizationError) as ctx:
            check_sqli_login(self.store, "example.com:443", "http://example.com")
        self.assertIn("目标不匹配", str(ctx.exception))

    def test_missing_proof_is_rejected(self):
        record = make_record(self.target)
        record.proof = ""
        self.assertTrue(any("授权证明" in item for item in record.problems()))

    def test_each_new_scope_is_gated(self):
        """新增的三类检查同样必须经过授权闸门。"""
        record = make_record(self.target, scopes=("sqli",))
        self.store.add(record)
        for call in (
            lambda: check_xss_search(self.store, self.target, self.base),
            lambda: check_ssrf_fetch(self.store, self.target, self.base),
            lambda: check_admin_role(self.store, self.target, self.base),
        ):
            with self.assertRaises(AuthorizationError):
                call()

    def test_valid_record_passes(self):
        self.store.add(make_record(self.target))
        record = self.store.assert_authorized(self.target, "sqli")
        self.assertEqual(record.owner, "测试所有者")


class TestVulnerableLab(LabMixin):
    fixed = False

    def test_sqli_is_detected(self):
        self.store.add(make_record(self.target))
        _, finding = check_sqli_login(self.store, self.target, self.base, evidence_store=self.evidence)
        self.assertEqual(finding.verdict, "vulnerable", finding.detail)
        self.assertEqual(finding.cwe, "CWE-89")

    def test_idor_is_detected(self):
        self.store.add(make_record(self.target))
        _, finding = check_idor_profile(self.store, self.target, self.base, evidence_store=self.evidence)
        self.assertEqual(finding.verdict, "vulnerable", finding.detail)

    def test_missing_headers_detected(self):
        _, findings = check_headers(self.base + "/", evidence_store=self.evidence)
        ids = {item.id for item in findings}
        self.assertIn("missing-header:Content-Security-Policy", ids)
        self.assertIn("info-disclosure:Server", ids)

    def test_xss_is_detected(self):
        self.store.add(make_record(self.target, scopes=("xss",)))
        _, finding = check_xss_search(self.store, self.target, self.base, evidence_store=self.evidence)
        self.assertEqual(finding.verdict, "vulnerable", finding.detail)
        self.assertEqual(finding.cwe, "CWE-79")

    def test_ssrf_is_detected(self):
        self.store.add(make_record(self.target, scopes=("ssrf",)))
        _, finding = check_ssrf_fetch(self.store, self.target, self.base, evidence_store=self.evidence)
        self.assertEqual(finding.verdict, "vulnerable", finding.detail)
        self.assertEqual(finding.cwe, "CWE-918")

    def test_client_supplied_role_bypass_detected(self):
        self.store.add(make_record(self.target, scopes=("privesc",)))
        _, finding = check_admin_role(self.store, self.target, self.base, evidence_store=self.evidence)
        self.assertEqual(finding.verdict, "vulnerable", finding.detail)
        self.assertEqual(finding.cwe, "CWE-285")

    def test_evidence_is_written(self):
        self.store.add(make_record(self.target))
        _, finding = check_sqli_login(self.store, self.target, self.base, evidence_store=self.evidence)
        path = self.evidence.directory / f"{finding.evidence_id}.json"
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "vulnerable")


class TestFixedLab(LabMixin):
    fixed = True

    def test_sqli_not_detected_after_fix(self):
        self.store.add(make_record(self.target))
        _, finding = check_sqli_login(self.store, self.target, self.base)
        self.assertEqual(finding.verdict, "not_vulnerable", finding.detail)

    def test_idor_not_detected_after_fix(self):
        self.store.add(make_record(self.target))
        _, finding = check_idor_profile(self.store, self.target, self.base)
        self.assertEqual(finding.verdict, "not_vulnerable", finding.detail)

    def test_headers_are_present(self):
        _, findings = check_headers(self.base + "/")
        self.assertEqual(findings, [], [item.id for item in findings])

    def test_xss_not_detected_after_fix(self):
        self.store.add(make_record(self.target, scopes=("xss",)))
        _, finding = check_xss_search(self.store, self.target, self.base)
        self.assertEqual(finding.verdict, "not_vulnerable", finding.detail)

    def test_ssrf_not_detected_after_fix(self):
        self.store.add(make_record(self.target, scopes=("ssrf",)))
        _, finding = check_ssrf_fetch(self.store, self.target, self.base)
        self.assertEqual(finding.verdict, "not_vulnerable", finding.detail)

    def test_client_supplied_role_rejected_after_fix(self):
        self.store.add(make_record(self.target, scopes=("privesc",)))
        _, finding = check_admin_role(self.store, self.target, self.base)
        self.assertEqual(finding.verdict, "not_vulnerable", finding.detail)


class TestAnalysisUnits(unittest.TestCase):
    def test_header_analysis_flags_version_only(self):
        findings = analyze_headers({"Server": "nginx/1.25.3"}, is_https=False)
        self.assertTrue(any(item["id"] == "info-disclosure:Server" for item in findings))

        findings = analyze_headers({"Server": "webserver"}, is_https=False)
        self.assertFalse(any(item["id"] == "info-disclosure:Server" for item in findings))

    def test_hsts_only_for_https(self):
        http_findings = analyze_headers({}, is_https=False)
        https_findings = analyze_headers({}, is_https=True)
        self.assertFalse(any("Strict-Transport" in item["id"] for item in http_findings))
        self.assertTrue(any("Strict-Transport" in item["id"] for item in https_findings))

    def test_fingerprint_has_source(self):
        hits = fingerprint({"Server": "AcmeApp/0.9.3"}, "<p>Powered by AcmeApp v0.9.3</p>")
        self.assertTrue(hits)
        for item in hits:
            self.assertTrue(item["source"])
            self.assertTrue(item["match"])


class TestRedaction(unittest.TestCase):
    def test_sensitive_keys_are_dropped(self):
        payload = redact(
            {"username": "alice", "password": "pw-alice", "Cookie": "sid=abc", "nested": {"token": "t"}}
        )
        self.assertEqual(payload["password"], "[REDACTED]")
        self.assertEqual(payload["Cookie"], "[REDACTED]")
        self.assertEqual(payload["nested"]["token"], "[REDACTED]")
        self.assertEqual(payload["username"], "alice")

    def test_bearer_and_query_are_scrubbed(self):
        text = redact("Authorization: Bearer abcdef1234567890 &password=pw-alice")
        self.assertNotIn("abcdef1234567890", text)
        self.assertNotIn("pw-alice", text)


class TestReport(unittest.TestCase):
    def _finding(self, fid: str, verdict: str) -> Finding:
        return Finding(
            id=fid, title=fid, cwe="CWE-000", severity="low", verdict=verdict, impact="", fix=""
        )

    def test_count_fixed_matches_by_id_not_index(self):
        before = [self._finding("a", "vulnerable"), self._finding("b", "vulnerable")]
        after = [self._finding("b", "not_vulnerable")]  # 缺了 a，且顺序不同
        self.assertEqual(count_fixed(before, after), 2)

    def test_inconclusive_is_not_counted_as_fixed(self):
        before = [self._finding("a", "vulnerable")]
        after = [self._finding("a", "inconclusive")]
        self.assertEqual(count_fixed(before, after), 1)  # 不再 vulnerable 即视为已处理

    def test_inconclusive_section_present(self):
        record = make_record("127.0.0.1:1")
        report = build_report(
            title="t",
            target="127.0.0.1:1",
            authorization=record,
            recon_before=type("R", (), {"status": 200, "headers": {}, "tech": [], "to_dict": lambda self: {}})(),
            findings_before=[self._finding("x", "vulnerable")],
            recon_after=type("R", (), {"status": 200, "headers": {}, "tech": [], "to_dict": lambda self: {}})(),
            findings_after=[self._finding("x", "inconclusive")],
            gate_selfcheck={"blocked": "是", "reason": "测试"},
            evidence_dir="evidence",
        )
        self.assertIn("未确认项", report)
        self.assertIn("无法判定", report)


class TestTaskEngine(LabMixin):
    """文档「最高优先级模块」：自主执行 + 不凭声称的完成校验。"""

    def _engine(self):
        from secengine.tasks import TaskEngine

        return TaskEngine(self.store, self.evidence, self._tmp / "out")

    def test_plan_only_includes_authorized_active_steps(self):
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        kinds = [item.kind for item in run.steps]
        # 没有授权记录 → 只有被动步骤 + report
        self.assertNotIn("sqli", kinds)
        self.assertIn("recon", kinds)
        self.assertIn("headers", kinds)
        self.assertIn("report", kinds)

    def test_plan_includes_active_steps_when_authorized(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        kinds = [item.kind for item in run.steps]
        for scope in ("sqli", "idor"):
            self.assertIn(scope, kinds)

    def test_autonomous_run_completes_and_records_real_tools(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        self.assertEqual(run.status, "run_complete")
        for step in run.steps:
            self.assertEqual(step.status, "done", f"{step.kind}: {step.error}")
            self.assertTrue(step.tool, f"{step.kind} 没有记录真实工具名")
            self.assertTrue(step.started_at and step.ended_at)
        # 主动步骤必须留下证据 id
        active = [item for item in run.steps if item.kind in ("sqli", "idor")]
        for step in active:
            self.assertTrue(step.evidence_ids)

    def test_verify_passes_for_complete_task(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        report = engine.out_dir / "task-report.md"
        ok, problems = engine.verify(run, report)
        self.assertTrue(ok, problems)
        self.assertEqual(run.status, "completed")
        self.assertIn("全部校验项通过", run.verification)

    def test_verify_fails_when_report_is_missing(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        ok, problems = engine.verify(run, engine.out_dir / "nope.md")
        self.assertFalse(ok)
        self.assertTrue(any("报告文件不存在" in item for item in problems))
        self.assertEqual(run.status, "failed")

    def test_verify_fails_when_evidence_deleted_from_disk(self):
        """完成校验必须看磁盘，而不是看账本里的自述。"""
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        # 删掉一条真实证据，账本仍声称它存在
        for path in self.evidence.directory.glob("ev_*.json"):
            path.unlink()
            break
        ok, problems = engine.verify(run, engine.out_dir / "task-report.md")
        self.assertFalse(ok)
        self.assertTrue(any("不在磁盘上" in item for item in problems))

    def test_unauthorized_step_blocks_and_pauses(self):
        from secengine.tasks import StepRecord, TaskRun

        engine = self._engine()
        run = TaskRun(
            task_id="task-manual",
            goal="未授权目标的主动测试",
            target=self.target,
            base_url=self.base,
            created_at="",
            updated_at="",
            steps=[StepRecord(index=0, kind="sqli")],
        )
        engine.run(run)
        self.assertEqual(run.steps[0].status, "blocked")
        self.assertIn("缺少有效授权", run.steps[0].error)
        self.assertEqual(run.status, "paused")

    def test_resume_skips_completed_steps(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        task_id = run.task_id
        before = {item.kind: item.attempts for item in run.steps}

        resumed = engine.resume(task_id)
        # 已完成的任务不应重复执行
        self.assertEqual(resumed.status, "run_complete")
        after = {item.kind: item.attempts for item in resumed.steps}
        self.assertEqual(before, after)

    def test_ledger_file_is_written(self):
        self.store.add(make_record(self.target))
        engine = self._engine()
        run = engine.plan("全面检查", self.target, self.base)
        engine.run(run)
        ledger = engine.ledger_dir / f"{run.task_id}.json"
        self.assertTrue(ledger.exists())
        payload = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["steps"]), len(run.steps))
        self.assertTrue(all(step["tool"] for step in payload["steps"]))


class TestEndToEndAcceptance(unittest.TestCase):
    """文档验收标准：真实执行完整闭环，而不是只生成界面。"""

    def test_demo_produces_real_before_after_report(self):
        from secengine.cli import main as cli_main

        tmp = Path(tempfile.mkdtemp(prefix="secengine-e2e-"))
        try:
            out = tmp / "report.md"
            code = cli_main(["demo", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            # 三个必需要素
            self.assertIn("授权闸门自检", text)
            self.assertIn("修复前后对比", text)
            self.assertIn("已修复", text)
            self.assertIn("CWE-89", text)
            self.assertIn("CWE-639", text)
            # 证据必须真的落盘
            evidence_files = list((tmp / "evidence").rglob("ev_*.json"))
            self.assertGreaterEqual(len(evidence_files), 4)
            # 报告不得把未验证内容写成确认结论
            self.assertIn("未确认项", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
