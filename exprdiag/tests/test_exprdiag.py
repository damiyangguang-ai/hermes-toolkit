"""ExprDiag 单元测试。

重点覆盖三类"错了会误导人"的行为：
  1. 假阳性（把版本号 major/minor、工具同意 consent 当成内容限制）
  2. 假阴性（只扫拼装器、漏掉真正的提示词文件）
  3. 越界（把"露骨探测"混进能力矩阵）
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from exprdiag.compat import (
    EXCLUDED_PROBES,
    PROBE_CLASSES,
    MockProvider,
    OpenAICompatProvider,
    ProviderResult,
    classify_provider_result,
    render_matrix,
    run_matrix,
)
from exprdiag.inspect import (
    classify_refusal,
    detect_prompt_conflicts,
    diagnose,
    discover_prompt_files,
    inspect_persona_layers,
    render_markdown,
    scan_text_for_rules,
)


class TempMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="exprdiag-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRuleScanning(unittest.TestCase):
    def test_detects_explicit_restriction(self):
        hits = scan_text_for_rules("不得生成色情内容", layer="soul", path="x")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].confidence, "high")

    def test_ignores_plain_personality_words(self):
        """「性格」「属性」里含「性」，不能因此判成内容限制。"""
        text = "性格特点：温暖有力，既有女性的细腻温暖。"
        self.assertEqual(scan_text_for_rules(text, layer="soul", path="x"), [])

    def test_ignores_version_tuple_major_minor(self):
        """真实假阳性：``(major, minor) of a Gemini model id`` 被判成未成年条款。"""
        text = '"""(major, minor) of a Gemini model id: gemini-4-argon -> (4, 0)"""'
        self.assertEqual(scan_text_for_rules(text, layer="system_prompt", path="x"), [])

    def test_ignores_tool_consent(self):
        """真实假阳性：工具调用里的 NOT consented 不是内容政策。"""
        text = 'if "NOT consented" in error: outcome += "; do not retry"'
        self.assertEqual(scan_text_for_rules(text, layer="system_prompt", path="x"), [])

    def test_reports_line_numbers(self):
        text = "第一行\n不得生成露骨描写\n第三行"
        hits = scan_text_for_rules(text, layer="soul", path="x")
        self.assertEqual(hits[0].line, 2)


class TestPromptDiscovery(TempMixin):
    def test_finds_real_prompt_container(self):
        install = self.tmp / "install"
        (install / "agent").mkdir(parents=True)
        prompt_file = install / "agent" / "prompt_builder.py"
        prompt_file.write_text(
            'DEFAULT = """\nYou are Hermes, a helpful agent.\n' + ("你是助手，必须遵守约定。\n" * 30) + '"""\n',
            encoding="utf-8",
        )
        found = discover_prompt_files(install)
        self.assertIn(prompt_file, found)

    def test_excludes_plain_code_file(self):
        """含 'GUIDANCE' 字样但只是业务代码的文件，不应被当成提示层。"""
        install = self.tmp / "install"
        (install / "agent").mkdir(parents=True)
        code = install / "agent" / "auxiliary_client.py"
        code.write_text(
            "import os\n\n# GUIDANCE constants are imported elsewhere\n"
            + ("def f():\n    return 1\n" * 200),
            encoding="utf-8",
        )
        self.assertNotIn(code, discover_prompt_files(install))

    def test_inspects_user_soul_layer(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / "SOUL.md").write_text("你是一个助手。\n", encoding="utf-8")
        result = inspect_persona_layers(home, None)
        soul = [item for item in result["layers"] if item["layer"] == "soul"]
        self.assertTrue(soul and soul[0]["exists"])


class TestConflictDetection(TempMixin):
    def test_detects_opposite_instructions_across_layers(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / "SOUL.md").write_text("允许生成成人内容的角色扮演。\n", encoding="utf-8")
        install = self.tmp / "install"
        (install / "agent").mkdir(parents=True)
        # 必须构成"真实的提示词容器"：含提示词特征词 + 足够长的正文，
        # 否则 discover_prompt_files 不会把它当成提示层（那一关也会拦下假的）。
        body = 'P = """\n你是助手。\n禁止生成成人内容。\n' + ("你必须遵守约定。\n" * 60) + '"""\n'
        (install / "agent" / "prompt_builder.py").write_text(body, encoding="utf-8")
        result = inspect_persona_layers(home, install)
        topics = [item["topic"] for item in result["conflicts"]]
        self.assertIn("成人内容", topics)


class TestRefusalClassification(unittest.TestCase):
    def test_provider_marker_wins(self):
        result = classify_refusal("error: data_inspection_failed")
        self.assertEqual(result["layer"], "provider")
        self.assertEqual(result["confidence"], "high")

    def test_local_marker(self):
        self.assertEqual(classify_refusal("blocked_by_hardline: rm -rf /")["layer"], "local")

    def test_model_judgment_when_request_sent(self):
        result = classify_refusal("抱歉，我不能协助这个请求", request_sent=True)
        self.assertEqual(result["layer"], "model_judgment")

    def test_unknown_without_evidence(self):
        result = classify_refusal("抱歉，我不能协助这个请求")
        self.assertEqual(result["layer"], "unknown")
        self.assertIn("不猜测", result["reason"])


class TestCompatibilityMatrix(unittest.TestCase):
    def test_mock_matrix_all_available(self):
        matrix = run_matrix([MockProvider()])
        providers = matrix["providers"]["mock"]
        for probe in PROBE_CLASSES:
            entry = providers[probe.key]
            if probe.expectation == "refused_ok":
                self.assertEqual(entry["verdict"], "安全行为正确")
            else:
                self.assertEqual(entry["verdict"], "可用", f"{probe.key}: {entry}")

    def test_filtered_mock_is_reported_per_layer(self):
        matrix = run_matrix([MockProvider(filtered={"romance_dialogue"})])
        entry = matrix["providers"]["mock"]["romance_dialogue"]
        self.assertEqual(entry["status"], "filtered")
        self.assertEqual(entry["verdict"], "被拦截（provider 策略）")

    def test_unsafe_model_is_flagged(self):
        """模型照写未成年人内容必须被标出来，不能被算作"可用"。"""
        class UnsafeProvider(MockProvider):
            name = "unsafe"

            def complete(self, prompt: str) -> ProviderResult:
                return ProviderResult(status="completed", text="（不安全的输出）", latency_ms=1)

        matrix = run_matrix([UnsafeProvider()])
        entry = matrix["providers"]["unsafe"][PROBE_CLASSES[-1].key]
        self.assertIn("未拒绝", entry["verdict"])

    def test_excluded_probes_are_documented(self):
        text = render_matrix(run_matrix([MockProvider()]))
        for item in EXCLUDED_PROBES:
            self.assertIn(item["item"], text)

    def test_openai_provider_requires_env_key(self):
        provider = OpenAICompatProvider(
            name="t", base_url="http://127.0.0.1:1/v1", model="m", api_key_env="EXPRDIAG_TEST_NO_SUCH_KEY"
        )
        with self.assertRaises(RuntimeError):
            provider.complete("hi")

    def test_classify_provider_result_splits_layers(self):
        self.assertEqual(classify_provider_result("", "content_filter", 200), "filtered")
        self.assertEqual(classify_provider_result("抱歉，我不能协助", "", 200), "refused")
        self.assertEqual(classify_provider_result("正常回答", "", 200), "completed")
        self.assertEqual(classify_provider_result("", "", 500), "error")


class TestReportRendering(unittest.TestCase):
    def test_report_marks_unknowns(self):
        tmp = Path(tempfile.mkdtemp(prefix="exprdiag-report-"))
        try:
            home = tmp / "home"
            home.mkdir()
            (home / "SOUL.md").write_text("你是一个助手。\n", encoding="utf-8")
            report = diagnose(home, None)
            text = render_markdown(report)
            self.assertIn("未确认项", text)
            self.assertIn("Unknown", text)
            self.assertIn("只读", text)
            self.assertIn("不负责让被拒内容通过", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
