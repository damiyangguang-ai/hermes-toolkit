"""HFK 自动化测试。

运行方式（在项目根目录）：
    python -m unittest discover -s tests -v

全部测试都在临时沙箱中执行，不会触碰真实 Hermes 配置。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hfk.config.keypath import (
    get_nested,
    greedy_literal_match,
    join_key_path,
    split_key_path,
)
from hfk.engine import patch
from hfk.errors import ConfigValidationError, SignatureError, UnsupportedVersionError
from hfk.facade import HFK
from hfk.testing import make_sandbox
from hfk.util import sha256_file


class TempDirMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._work = Path(tempfile.mkdtemp(prefix="hfk-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._work, ignore_errors=True)

    def sandbox(self, name: str, **kwargs):
        return make_sandbox(self._work / name, **kwargs)


class TestKeyPath(unittest.TestCase):
    def test_split_plain(self):
        self.assertEqual(split_key_path("a.b.c"), ["a", "b", "c"])

    def test_split_escaped_dot(self):
        self.assertEqual(split_key_path(r"providers.myprov\.ai.model"), ["providers", "myprov.ai", "model"])

    def test_join_escapes(self):
        self.assertEqual(join_key_path(["a", "b.c"]), r"a.b\.c")

    def test_greedy_literal_prefers_existing_dotted_key(self):
        container = {"grok-4.6": {"ctx": 1}, "grok-4": {"ctx": 2}}
        self.assertEqual(greedy_literal_match(container, ["grok-4.6", "ctx"]), ("grok-4.6", 1))

    def test_get_nested_literal_key(self):
        document = {"providers": {"glm-5.3": {"context_length": 200}}}
        found, value = get_nested(document, "providers.glm-5.3.context_length")
        self.assertTrue(found)
        self.assertEqual(value, 200)

    def test_get_nested_missing(self):
        self.assertEqual(get_nested({"a": 1}, "a.b"), (False, None))


class TestYamlIO(TempDirMixin):
    def test_off_is_quoted_and_reads_back_as_string(self):
        box = self.sandbox("off", config_text="approvals:\n  mode: smart\n  timeout: 60\n")
        patch.update_keys(box.config_path, {"approvals.mode": "off"})
        text = box.config_path.read_text(encoding="utf-8")
        self.assertIn('"off"', text)
        from hfk.config.yaml_io import plain_yaml

        document = plain_yaml().load(text)
        self.assertEqual(document["approvals"]["mode"], "off")

    def test_comments_and_crlf_preserved(self):
        box = self.sandbox("comments")
        original = box.config_path.read_text(encoding="utf-8")
        patch.update_keys(box.config_path, {"approvals.timeout": 120})
        after = box.config_path.read_text(encoding="utf-8")
        self.assertEqual(original.count("#"), after.count("#"))
        raw = box.config_path.read_bytes()
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0)

    def test_creates_missing_field_then_deletes(self):
        box = self.sandbox("create")
        patch.update_keys(box.config_path, {"approvals.brand_new": 7})
        from hfk.config.yaml_io import plain_yaml

        document = plain_yaml().load(box.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["approvals"]["brand_new"], 7)
        patch.update_keys(box.config_path, {"approvals.brand_new": None})
        document = plain_yaml().load(box.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("brand_new", document["approvals"])

    def test_unchanged_write_is_stable(self):
        box = self.sandbox("stable")
        before = box.config_path.read_bytes()
        patch.update_keys(box.config_path, {"approvals.timeout": 60})
        self.assertEqual(box.config_path.read_bytes(), before)


class TestEngine(TempDirMixin):
    def test_apply_restore_is_byte_exact(self):
        box = self.sandbox("fidelity")
        hfk = HFK(box.env())
        before = box.config_path.read_bytes()
        tx = hfk.apply(preset="balanced")
        self.assertNotEqual(box.config_path.read_bytes(), before)
        report = hfk.restore(tx["transaction_id"])
        self.assertEqual(report["status"], "restored")
        self.assertEqual(box.config_path.read_bytes(), before)

    def test_no_change_raises(self):
        box = self.sandbox("nochange")
        hfk = HFK(box.env())
        with self.assertRaises(ConfigValidationError):
            hfk.apply(items={"approvals.timeout": 60})

    def test_conflict_preserves_user_value(self):
        box = self.sandbox("conflict")
        hfk = HFK(box.env())
        hfk.apply(items={"approvals.timeout": 120})
        patch.update_keys(box.config_path, {"approvals.timeout": 180})
        report = hfk.restore()
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["status"], "partially_restored")
        from hfk.config.yaml_io import plain_yaml

        document = plain_yaml().load(box.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["approvals"]["timeout"], 180)

    def test_multiple_applies_restore_in_reverse(self):
        box = self.sandbox("multi")
        hfk = HFK(box.env())
        before = box.config_path.read_bytes()
        first = hfk.apply(items={"approvals.timeout": 120})
        second = hfk.apply(items={"approvals.timeout": 200})
        report = hfk.restore()
        self.assertEqual(report["transaction_ids"], [second["transaction_id"], first["transaction_id"]])
        self.assertEqual(box.config_path.read_bytes(), before)

    def test_signature_mismatch_blocks_apply(self):
        box = self.sandbox("signature")
        hfk = HFK(box.env())
        plan = patch.build_plan(box.env(), {"approvals.timeout": 120})
        # 预览之后文件被外部修改
        patch.update_keys(box.config_path, {"approvals.timeout": 90})
        with self.assertRaises(SignatureError):
            patch.apply(box.env(), plan)

    def test_unsupported_version_blocked(self):
        box = self.sandbox("unsupported", version="9.9.9")
        hfk = HFK(box.env())
        self.assertFalse(hfk.compat()["supported"])
        with self.assertRaises(UnsupportedVersionError):
            hfk.apply(items={"approvals.timeout": 120})

    def test_rollback_on_verification_failure(self):
        """写入后校验失败必须回滚到原始字节，且事务记为 failed。"""
        box = self.sandbox("rollback")
        hfk = HFK(box.env())
        before = box.config_path.read_bytes()

        original_update_keys = patch.update_keys

        def corrupted_update_keys(path, updates):
            # 故意写入一个错误值，让回读校验失败
            original_update_keys(path, {"approvals.timeout": 999})

        patch.update_keys = corrupted_update_keys
        try:
            with self.assertRaises(Exception):
                hfk.apply(items={"approvals.timeout": 120})
        finally:
            patch.update_keys = original_update_keys

        self.assertEqual(box.config_path.read_bytes(), before)
        transactions = hfk.transactions()["transactions"]
        self.assertTrue(any(tx["status"] == "failed" for tx in transactions))
        # 失败也必须留下备份
        self.assertGreaterEqual(len(hfk.backups()["files"]), 1)

    def test_refuses_non_mapping_parent(self):
        box = self.sandbox("scalarparent", config_text="approvals: broken\n")
        hfk = HFK(box.env())
        plan = patch.build_plan(box.env(), {"approvals.timeout": 120})
        self.assertTrue(plan.errors)
        with self.assertRaises(ConfigValidationError):
            hfk.apply(items={"approvals.timeout": 120})

    def test_uninstall_keeps_backups(self):
        box = self.sandbox("uninstall")
        hfk = HFK(box.env())
        before = box.config_path.read_bytes()
        tx = hfk.apply(items={"approvals.timeout": 120})
        report = hfk.uninstall()
        self.assertEqual(box.config_path.read_bytes(), before)
        self.assertTrue(Path(tx["backup_path"]).exists())
        self.assertTrue(Path(report["record_path"]).exists())

    def test_backup_hash_matches_original(self):
        box = self.sandbox("backuphash")
        hfk = HFK(box.env())
        original_sha = sha256_file(box.config_path)
        tx = hfk.apply(items={"approvals.timeout": 120})
        self.assertEqual(tx["backup_sha256"], original_sha)


class TestService(TempDirMixin):
    def test_requires_token_and_applies(self):
        import json
        import urllib.error
        import urllib.request

        from hfk.service.app import HFKService

        box = self.sandbox("service")
        hfk = HFK(box.env())
        service = HFKService(hfk)
        service.start()
        try:
            base = service.base_url

            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base}/api/status", timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            request = urllib.request.Request(
                f"{base}/api/status", headers={"X-HFK-Token": service.token}
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)

            body = json.dumps({"items": {"approvals.timeout": 120}}).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/api/apply",
                data=body,
                headers={"X-HFK-Token": service.token, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("transaction_id", payload)
        finally:
            service.stop()


class TestSecretHygiene(TempDirMixin):
    """服务对外投影绝不能泄露配置里的秘密。"""

    def test_managed_config_projection_excludes_foreign_keys(self):
        box = self.sandbox(
            "secrets",
            config_text=(
                "approvals:\n"
                "  mode: smart\n"
                "  timeout: 60\n"
                "auxiliary:\n"
                "  approval:\n"
                "    api_key: sk-should-never-leak\n"
                "    provider: openai\n"
            ),
        )
        hfk = HFK(box.env())
        projection = hfk.managed_config()
        self.assertTrue(projection["readable"])
        serialized = __import__("json").dumps(projection, ensure_ascii=False)
        self.assertNotIn("sk-should-never-leak", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertIn("approvals.mode", projection["fields"])
        self.assertEqual(projection["fields"]["approvals.mode"]["value"], "smart")


class TestIntegrity(TempDirMixin):
    """数据完整性：宁可报错，也不要把「不知道」当成「没有」。"""

    def test_corrupt_transaction_file_raises(self):
        from hfk.errors import TransactionError

        box = self.sandbox("corrupt")
        hfk = HFK(box.env())
        store_path = box.home / "hfk" / "transactions" / "transactions.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(TransactionError):
            hfk.transactions()

    def test_restore_allowed_on_unsupported_version(self):
        """升级 Hermes 后仍必须能撤销旧补丁，否则用户会被卡住。"""
        box = self.sandbox("restore-unsupported")
        hfk = HFK(box.env())
        before = box.config_path.read_bytes()
        tx = hfk.apply(items={"approvals.timeout": 120})

        # 模拟 Hermes 升级：换掉 install-stamp 的版本号
        stamp_path = box.install_dir / "install-stamp.json"
        import json as _json

        stamp = _json.loads(stamp_path.read_text(encoding="utf-8"))
        stamp["baseVersion"] = stamp["displayVersion"] = "9.9.9"
        stamp_path.write_text(_json.dumps(stamp), encoding="utf-8")

        hfk2 = HFK(box.env())
        self.assertFalse(hfk2.compat()["supported"])
        report = hfk2.restore(tx["transaction_id"])
        self.assertEqual(report["status"], "restored")
        self.assertEqual(box.config_path.read_bytes(), before)
        self.assertTrue(any("未验证" in item for item in report["warnings"]))


class TestTokenFilePermissions(TempDirMixin):
    """令牌发现文件的权限必须真的收紧（Windows 上 chmod 无效，要用 ACL）。"""

    def test_discovery_file_is_not_inherited(self):
        import subprocess
        import sys

        from hfk.service.app import HFKService

        box = self.sandbox("tokenperm")
        hfk = HFK(box.env())
        service = HFKService(hfk)
        try:
            path = service.write_discovery_file()
            self.assertTrue(service._discovery_hardened, "收紧失败，说明 ACL 没生效")
            self.assertTrue(path.exists())
            # 自己必须还能读
            self.assertIn("token", path.read_text(encoding="utf-8"))

            if sys.platform == "win32":
                result = subprocess.run(
                    ["icacls", str(path)], capture_output=True, text=True, timeout=20, check=False
                )
                output = result.stdout
                # 关键断言：不再有继承标记 (I)
                self.assertNotIn("(I)", output, f"仍存在继承权限：\n{output}")
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
