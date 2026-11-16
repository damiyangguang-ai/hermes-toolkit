"""真机端到端验证（谨慎、可逆）。

分两段：

A. 在**真实 config.yaml 的副本**上跑完整闭环（apply → 校验 → restore → 字节比对）。
   这段完全无风险，验证 HFK 能正确处理真实文档（含全部注释与真实段落）。

B. 在**真实 config.yaml** 上做一次「可逆往返」：
   只动 ``approvals.timeout``（60 → 120），随后立刻 Restore，
   并断言文件哈希回到原始值。全程若出错，finally 里用备份字节强制回滚。
   仅当网关当前没有活跃 agent 时才执行，避免打断正在跑的任务。

最后用 **Hermes 自己的读取函数**确认文件在写入后仍是它能正常读的配置。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(r"<WORKSPACE>\Hermes-Freedom-Kit")
REPO = Path(r"<HERMES_HOME>\hermes-agent")
LIVE_HOME = Path(r"<HERMES_HOME>")
LIVE_CONFIG = LIVE_HOME / "config.yaml"

sys.path.insert(0, str(KIT))

from hfk.engine import patch  # noqa: E402
from hfk.env import detect  # noqa: E402
from hfk.facade import HFK  # noqa: E402
from hfk.testing import make_sandbox  # noqa: E402
from hfk.util import sha256_file  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {name}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def hermes_reads_config(config_path: Path) -> dict:
    """用 Hermes 自己的 fail-closed 读取函数校验该文件，返回 approvals 片段。"""
    code = (
        "import json,sys;"
        "from pathlib import Path;"
        "from hermes_cli.config import require_readable_config_before_write;"
        f"doc=require_readable_config_before_write(Path(r'{config_path}'));"
        "print(json.dumps({'approvals': doc.get('approvals'), 'top_keys': len(doc)}, default=str))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0:
        return {"error": (completed.stderr or "").strip()[-800:]}
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"error": f"解析输出失败: {exc}", "raw": completed.stdout[-400:]}


def part_a_real_document_copy() -> None:
    print("\nA. 真实配置副本上的完整闭环")
    work = Path(tempfile.mkdtemp(prefix="hfk-livecopy-"))
    try:
        box = make_sandbox(work / "box")
        shutil.copyfile(LIVE_CONFIG, box.config_path)
        before = box.config_path.read_bytes()
        print(f"  副本大小 {len(before)} 字节，哈希 {sha256_file(box.config_path)[:16]}…")

        hfk = HFK(box.env())
        plan = patch.build_plan(box.env(), {"approvals.timeout": 120, "approvals.mode": "smart"})
        changed = [c for c in plan.changes if c.changed]
        check("计划中恰好 1 项需要改动（mode 已是 smart）", len(changed) == 1, f"实际 {len(changed)}")

        tx = hfk.apply(items={"approvals.timeout": 120})
        after_bytes = box.config_path.read_bytes()
        check("写入后文件大小变化极小", abs(len(after_bytes) - len(before)) < 40,
              f"{len(before)} → {len(after_bytes)}")

        # 只应有 1 行差异
        left = before.decode("utf-8").splitlines()
        right = after_bytes.decode("utf-8").splitlines()
        diff_lines = sum(1 for a, b in zip(left, right) if a != b) if len(left) == len(right) else -1
        check("只有 1 行内容发生变化", diff_lines == 1, f"差异行 {diff_lines}")

        readback = hermes_reads_config(box.config_path)
        check("Hermes 自己的读取函数认可该文件", "error" not in readback, str(readback)[:300])
        check("Hermes 读到 timeout=120",
              readback.get("approvals", {}).get("timeout") == 120,
              str(readback.get("approvals"))[:200])

        hfk.restore(tx["transaction_id"])
        check("Restore 后与原始副本字节完全一致", box.config_path.read_bytes() == before)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def part_b_live_roundtrip() -> None:
    print("\nB. 真实配置文件上的可逆往返（approvals.timeout 60 → 120 → 60）")
    env = detect(home=str(LIVE_HOME))
    print(f"  Hermes {env.version} / Profile {env.profile} / Gateway "
          f"{'running pid ' + str(env.gateway.pid) if env.gateway.running else 'stopped'}")

    if env.gateway.running and (env.gateway.active_agents or 0) > 0:
        print(f"  跳过：网关有 {env.gateway.active_agents} 个活跃 agent，避免打断任务")
        return

    before = LIVE_CONFIG.read_bytes()
    before_sha = sha256_file(LIVE_CONFIG)
    print(f"  原始哈希 {before_sha}")

    hfk = HFK(env)
    tx_id = None
    backup_bytes = None
    try:
        tx = hfk.apply(items={"approvals.timeout": 120})
        tx_id = tx["transaction_id"]
        backup_bytes = Path(tx["backup_path"]).read_bytes()
        print(f"  已应用事务 {tx_id}")

        check("实时文件确实改变", sha256_file(LIVE_CONFIG) != before_sha)
        readback = hermes_reads_config(LIVE_CONFIG)
        check("Hermes 读取函数仍能解析实时配置", "error" not in readback, str(readback)[:300])
        check("Hermes 读到 timeout=120",
              readback.get("approvals", {}).get("timeout") == 120,
              str(readback.get("approvals"))[:200])
        check("Hermes 仍读到 mode=smart",
              readback.get("approvals", {}).get("mode") == "smart",
              str(readback.get("approvals"))[:200])

        report = hfk.restore(tx_id)
        check("Restore 报告 restored", report["status"] == "restored", str(report)[:300])
        rest_sha = sha256_file(LIVE_CONFIG)
        check("实时文件已回到原始哈希", rest_sha == before_sha,
              f"{before_sha} vs {rest_sha}")
        check("字节完全一致", LIVE_CONFIG.read_bytes() == before)

        readback = hermes_reads_config(LIVE_CONFIG)
        check("恢复后 Hermes 读到 timeout=60",
              readback.get("approvals", {}).get("timeout") == 60,
              str(readback.get("approvals"))[:200])
    finally:
        # 兜底：无论上面发生什么，只要文件不等于原始字节就用备份强制回滚
        if LIVE_CONFIG.read_bytes() != before:
            print("  ! 检测到实时文件未还原，用备份字节强制回滚")
            if backup_bytes is not None:
                from hfk.config.yaml_io import atomic_write_bytes

                atomic_write_bytes(LIVE_CONFIG, backup_bytes)
                print(f"  强制回滚后哈希 {sha256_file(LIVE_CONFIG)}")
        if LIVE_CONFIG.read_bytes() == before:
            print("  ✔ 实时配置最终与原始字节一致")
        else:
            failures.append("实时配置未能还原")


def main() -> int:
    print("=" * 68)
    print("HFK 真机端到端验证")
    print("=" * 68)
    part_a_real_document_copy()
    part_b_live_roundtrip()
    print("\n" + "=" * 68)
    if failures:
        print(f"失败项 {len(failures)}：")
        for item in failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
