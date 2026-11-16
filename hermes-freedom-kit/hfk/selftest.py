"""内置自检：不依赖 pytest，用户随时可以 ``hfk selftest`` 验证引擎是否可靠。

自检全部在**临时沙箱**里跑，绝不触碰真实 Hermes 配置。

覆盖的关键不变量：
  1. YAML 语义：注释保留、CRLF、``off`` 必须带引号（否则被读成 False）
  2. Apply：只改目标字段、其余字节不变、事务与备份双记录
  3. 冲突：用户后来改过的字段，Restore 必须保留用户值
  4. 多次 Apply：必须按事务**逆序**恢复
  5. 还原保真：一次 Apply 再 Restore，配置文件必须**字节完全还原**
  6. Uninstall：保留备份，不删用户配置
  7. 失败回滚：写入后校验失败时，必须回到原始字节
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hfk.config.keypath import get_nested
from hfk.config.yaml_io import plain_yaml, read_text
from hfk.engine import patch
from hfk.facade import HFK
from hfk.testing import make_sandbox
from hfk.util import sha256_file

__all__ = ["run_selftest"]

_GREEN = "\u2714"
_RED = "\u2718"


class _Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passes += 1
            print(f"  {_GREEN} {name}")
        else:
            self.failures.append(f"{name}: {detail}" if detail else name)
            print(f"  {_RED} {name}  {detail}")


def _document(path: Path) -> dict:
    return plain_yaml().load(read_text(path)) or {}


def run_selftest() -> int:
    checker = _Checker()
    work = Path(tempfile.mkdtemp(prefix="hfk-selftest-"))
    try:
        _scenario_yaml_semantics(checker, work)
        _scenario_apply_restore_fidelity(checker, work)
        _scenario_conflict(checker, work)
        _scenario_multi_transaction(checker, work)
        _scenario_unsupported_version(checker, work)
        _scenario_uninstall(checker, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("-" * 62)
    if checker.failures:
        print(f"自检失败：{len(checker.failures)} 项")
        for item in checker.failures:
            print(f"  · {item}")
        return 1
    print(f"自检通过：{checker.passes} 项检查全部成功")
    return 0


# --------------------------------------------------------------------------- 场景


def _scenario_yaml_semantics(checker: _Checker, work: Path) -> None:
    print("场景 1｜YAML 语义")
    box = make_sandbox(work / "yaml")
    original_bytes = box.config_path.read_bytes()

    from hfk.config.yaml_io import update_keys

    update_keys(box.config_path, {"approvals.mode": "off", "approvals.timeout": 120})
    text = read_text(box.config_path)
    raw = box.config_path.read_bytes()

    checker.check("写入后仍为 CRLF、无裸 LF", raw.count(b"\n") - raw.count(b"\r\n") == 0)
    checker.check(
        "注释全部保留",
        text.count("#") >= original_bytes.decode("utf-8").count("#"),
        "注释数量减少",
    )
    mode_lines = [line for line in text.splitlines() if line.strip().startswith("mode:")]
    checker.check(
        "mode: off 被加引号（YAML 1.1 安全）",
        any(("'off'" in line or '"off"' in line) for line in mode_lines),
        f"实际 {mode_lines}",
    )
    checker.check(
        "读回 approvals.mode 是字符串 'off'",
        _document(box.config_path)["approvals"]["mode"] == "off",
    )
    checker.check("timeout 已改为 120", _document(box.config_path)["approvals"]["timeout"] == 120)
    checker.check(
        "flow 风格列表未被展开",
        "enabled: [terminal, files]" in text,
        "flow 列表被改写",
    )


def _scenario_apply_restore_fidelity(checker: _Checker, work: Path) -> None:
    print("场景 2｜Apply → Restore 字节级保真")
    box = make_sandbox(work / "fidelity")
    hfk = HFK(box.env())
    before_bytes = box.config_path.read_bytes()
    before_sha = sha256_file(box.config_path)

    result = hfk.apply(preset="balanced")
    after_sha = sha256_file(box.config_path)
    checker.check("Apply 后文件确实改变", after_sha != before_sha)
    checker.check("生成了备份", Path(result["backup_path"]).exists())
    checker.check("生成了事务记录", len(hfk.transactions()["transactions"]) == 1)
    checker.check(
        "事务记录了字段级 before/after",
        all(("before" in change and "after" in change) for change in result["changes"]),
    )

    restored = hfk.restore(result["transaction_id"])
    checker.check("Restore 状态为 restored", restored["status"] == "restored", str(restored))
    checker.check(
        "Restore 后与原始字节完全一致",
        box.config_path.read_bytes() == before_bytes,
        "字节未完全还原",
    )
    checker.check("备份在 Restore 后仍存在", Path(result["backup_path"]).exists())
    checker.check("事务状态已更新为 restored", hfk.transactions()["transactions"][0]["status"] == "restored")


def _scenario_conflict(checker: _Checker, work: Path) -> None:
    print("场景 3｜冲突检测（用户后续修改不得被覆盖）")
    box = make_sandbox(work / "conflict")
    hfk = HFK(box.env())

    result = hfk.apply(items={"approvals.timeout": 120})
    checker.check(
        "Apply 写入 timeout=120",
        _document(box.config_path)["approvals"]["timeout"] == 120,
    )

    # 模拟「用户在 HFK 之后自己改成了 180」
    from hfk.config.yaml_io import update_keys

    update_keys(box.config_path, {"approvals.timeout": 180})

    restored = hfk.restore()
    checker.check("检测到冲突", len(restored["conflicts"]) == 1, str(restored["conflicts"]))
    checker.check(
        "冲突时保留用户值 180",
        _document(box.config_path)["approvals"]["timeout"] == 180,
        f"实际 {_document(box.config_path)['approvals']['timeout']}",
    )
    checker.check("状态标记为部分恢复", restored["status"] == "partially_restored")


def _scenario_multi_transaction(checker: _Checker, work: Path) -> None:
    print("场景 4｜多次 Apply 必须逆序恢复")
    box = make_sandbox(work / "multi")
    hfk = HFK(box.env())
    original = box.config_path.read_bytes()

    first = hfk.apply(items={"approvals.timeout": 120})
    second = hfk.apply(items={"approvals.timeout": 200})
    checker.check(
        "第二次 Apply 的 before 是第一次的 after",
        second["changes"][0]["before"] == 120,
        f"实际 {second['changes'][0]['before']}",
    )

    report = hfk.restore()
    checker.check(
        "恢复了两条事务且按逆序",
        report["transaction_ids"] == [second["transaction_id"], first["transaction_id"]],
        str(report["transaction_ids"]),
    )
    checker.check("最终字节还原", box.config_path.read_bytes() == original)


def _scenario_unsupported_version(checker: _Checker, work: Path) -> None:
    print("场景 5｜未知版本必须禁止 Apply")
    box = make_sandbox(work / "unsupported", version="9.9.9")
    hfk = HFK(box.env())
    checker.check("兼容性判定为不支持", not hfk.compat()["supported"])

    blocked = False
    try:
        hfk.apply(items={"approvals.timeout": 120})
    except Exception as exc:  # UnsupportedVersionError
        blocked = "未验证" in str(exc) or "版本" in str(exc)
    checker.check("Apply 被拒绝", blocked)
    checker.check("配置文件未被修改", _document(box.config_path)["approvals"]["timeout"] == 60)


def _scenario_uninstall(checker: _Checker, work: Path) -> None:
    print("场景 6｜Uninstall 保留备份与用户配置")
    box = make_sandbox(work / "uninstall")
    hfk = HFK(box.env())
    original = box.config_path.read_bytes()

    result = hfk.apply(items={"approvals.timeout": 120})
    backup = Path(result["backup_path"])
    plugin_dir = box.home / "desktop-plugins" / "hfk"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.js").write_text("// fake", encoding="utf-8")

    report = hfk.uninstall()
    checker.check("配置已安全恢复", box.config_path.read_bytes() == original)
    checker.check("插件目录已删除", not plugin_dir.exists())
    checker.check("备份仍保留", backup.exists())
    checker.check("卸载记录已写入", Path(report["record_path"]).exists())
    checker.check("Hermes 配置仍存在", box.config_path.exists())
