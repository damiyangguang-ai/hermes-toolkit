"""字段清单 / 兼容清单 / 预设 三者的一致性测试。

这一组测试防的是「文档漂移」：如果哪天有人往预设里塞了 B 类字段，
或者兼容清单与字段清单不一致，测试必须立刻失败。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hfk.compat import check, load_manifests
from hfk.profiles import PRESETS, preset_items
from hfk.schema import BLOCKED_FIELDS, CATALOGUE, get_spec, spec_paths, validate_value

_MANIFEST = Path(__file__).parent.parent / "hfk" / "compat" / "manifests" / "hermes-0.23.1.json"


class TestSchema(unittest.TestCase):
    def test_catalogue_paths_unique(self):
        paths = spec_paths()
        self.assertEqual(len(paths), len(set(paths)))

    def test_every_spec_has_evidence(self):
        for spec in CATALOGUE:
            self.assertTrue(spec.evidence, f"{spec.path} 缺少源码证据")
            self.assertTrue(spec.effect, f"{spec.path} 缺少作用说明")

    def test_no_secret_fields_in_catalogue(self):
        for spec in CATALOGUE:
            lowered = spec.path.lower()
            self.assertNotIn("api_key", lowered)
            self.assertNotIn("token", lowered)
            self.assertNotIn("password", lowered)

    def test_enum_values_are_authoritative(self):
        self.assertEqual(get_spec("approvals.mode").values, ("manual", "smart", "off"))
        for path in (
            "approvals.cron_mode",
            "approvals.single_query_mode",
            "approvals.unattended_mode",
        ):
            self.assertEqual(get_spec(path).values, ("deny", "approve"))

    def test_mode_default_is_smart_not_manual(self):
        # 实测默认值：不要按直觉写成 manual
        self.assertEqual(get_spec("approvals.mode").default, "smart")

    def test_validate_rejects_wrong_enum(self):
        self.assertTrue(validate_value("approvals.mode", "deny"))
        self.assertFalse(validate_value("approvals.mode", "off"))

    def test_validate_rejects_bool_as_int(self):
        self.assertTrue(validate_value("approvals.timeout", True))

    def test_validate_rejects_unknown_field(self):
        self.assertTrue(validate_value("approvals.not_a_field", 1))

    def test_blocked_fields_are_rejected(self):
        for path in BLOCKED_FIELDS:
            self.assertTrue(validate_value(path, "x"), f"{path} 应被拒绝")


class TestManifestCoverage(unittest.TestCase):
    def test_manifest_matches_catalogue(self):
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        manifest_fields = set(data["supported_fields"])
        catalogue = set(spec_paths())
        self.assertEqual(
            manifest_fields,
            catalogue,
            f"清单与字段表不一致：仅清单 {manifest_fields - catalogue}，仅字段表 {catalogue - manifest_fields}",
        )

    def test_exact_version_supported(self):
        result = check((0, 23, 1), "0.23.1")
        self.assertTrue(result.supported)
        self.assertTrue(result.exact)

    def test_same_family_supported_with_warning(self):
        result = check((0, 23, 7), "0.23.7")
        self.assertTrue(result.supported)
        self.assertFalse(result.exact)

    def test_other_version_unsupported(self):
        result = check((0, 24, 0), "0.24.0")
        self.assertFalse(result.supported)

    def test_unknown_version_unsupported(self):
        self.assertFalse(check(None, None).supported)


class TestPresets(unittest.TestCase):
    def test_default_preset_is_balanced(self):
        from hfk.schema import DEFAULT_PRESET

        self.assertEqual(DEFAULT_PRESET, "balanced")

    def test_presets_never_include_b_class(self):
        for key in PRESETS:
            for path in preset_items(key):
                spec = get_spec(path)
                self.assertIsNotNone(spec, f"{key} 引用了未知字段 {path}")
                self.assertEqual(spec.risk, "A", f"{key} 不应包含 B 类字段 {path}")

    def test_freedom_requires_acknowledgement(self):
        self.assertTrue(PRESETS["freedom"].requires_acknowledgement)

    def test_each_preset_entry_is_a_legal_value(self):
        for key in PRESETS:
            for path, value in preset_items(key).items():
                self.assertEqual(validate_value(path, value), [], f"{key}:{path}={value} 非法")


if __name__ == "__main__":
    unittest.main()
