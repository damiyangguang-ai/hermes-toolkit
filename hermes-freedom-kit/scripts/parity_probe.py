"""开发期探针：验证 HFK 的 YAML 写入与 Hermes 自身写入器**字节一致**。

运行方式（用 Hermes 的 venv 解释器，工作目录 = Hermes 仓库根）：

    $env:PYTHONPATH="E:\\harness\\soudushi\\Hermes-Freedom-Kit"
    python.exe E:\\harness\\soudushi\\Hermes-Freedom-Kit\\scripts\\parity_probe.py

它做三件事：
  1. 在临时副本上，用 HFK 的 update_key 改一个字段；
  2. 在另一份临时副本上，用 Hermes 的 atomic_roundtrip_yaml_update 改同一字段；
  3. 比较两份产物的字节；并检查 CRLF、注释保留、``off`` 的 YAML 1.1 引号语义。
"""

from __future__ import annotations

import difflib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _live_config() -> Path:
    """定位真实 config.yaml：环境变量优先，其次按 LOCALAPPDATA 推导。

    不写死本机路径——否则仓库在别人机器上必然失败（这是真实踩过的坑）。
    """
    override = os.environ.get("HFK_HERMES_HOME")
    if override:
        return Path(override) / "config.yaml"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes" / "config.yaml"
    return Path("__no_hermes_home__") / "config.yaml"


SOURCE = _live_config()
# 让 Hermes 仓库根可被导入（探针需要它的写入器做对照）
sys.path.insert(0, os.getcwd())

from hfk.config.yaml_io import load_document, plain_yaml, read_bytes, update_key  # noqa: E402

SOURCE = _live_config()
# 例如：C:\Users\<you>\AppData\Local\hermes\config.yaml


def _line_stats(raw: bytes) -> tuple[int, int]:
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    return crlf, bare_lf


def _changed_lines(a: bytes, b: bytes) -> tuple[int, list[str]]:
    left = a.decode("utf-8").splitlines()
    right = b.decode("utf-8").splitlines()
    if len(left) != len(right):
        return -1, [f"<行数不同 {len(left)} -> {len(right)}>"]
    diffs = [f"  - {x!r}\n  + {y!r}" for x, y in zip(left, right) if x != y]
    return len(diffs), diffs


def main() -> int:
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="hfk-parity-"))
    hfk_copy = work / "hfk.yaml"
    hermes_copy = work / "hermes.yaml"
    shutil.copyfile(SOURCE, hfk_copy)
    shutil.copyfile(SOURCE, hermes_copy)
    before = read_bytes(hfk_copy)

    # ---- 1. HFK 写入
    update_key(hfk_copy, "approvals.timeout", 120)
    hfk_after = read_bytes(hfk_copy)

    # ---- 2. Hermes 自身写入器
    try:
        from utils import atomic_roundtrip_yaml_update

        atomic_roundtrip_yaml_update(hermes_copy, "approvals.timeout", 120)
        hermes_after = read_bytes(hermes_copy)
        hermes_available = True
    except Exception as exc:  # pragma: no cover - 探针环境问题
        hermes_available = False
        hermes_after = b""
        print(f"[warn] 无法调用 Hermes 写入器: {exc}")

    # ---- 3. 断言
    crlf_before, lf_before = _line_stats(before)
    crlf_after, lf_after = _line_stats(hfk_after)
    print(f"原文件换行: CRLF={crlf_before} 裸LF={lf_before}")
    print(f"HFK 写入后: CRLF={crlf_after} 裸LF={lf_after}")
    if lf_after:
        failures.append(f"HFK 写入后出现 {lf_after} 个裸 LF（应为全 CRLF）")

    changed, diffs = _changed_lines(before, hfk_after)
    print(f"HFK 改动行数: {changed}")
    if changed != 1:
        print("---- 统一 diff（最多 80 行）----")
        diff = list(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                hfk_after.decode("utf-8").splitlines(keepends=True),
                fromfile="before",
                tofile="hfk-after",
                n=1,
            )
        )
        for line in diff[:80]:
            print(line.rstrip("\r\n"))
        print("---- diff 结束 ----")
        failures.append(f"应只改动 1 行，实际 {changed}")

    if hermes_available:
        if hermes_after == hfk_after:
            print("[OK] 与 Hermes 写入器字节完全一致")
        else:
            failures.append("与 Hermes 写入器字节不一致")
            changed2, diffs2 = _changed_lines(hermes_after, hfk_after)
            print(f"[FAIL] 与 Hermes 产物差异行数: {changed2}")
            for line in diffs2[:20]:
                print(line)

    # ---- 4. YAML 1.1 歧义字符串：mode: off 必须带引号且读回为字符串
    off_copy = work / "off.yaml"
    shutil.copyfile(SOURCE, off_copy)
    update_key(off_copy, "approvals.mode", "off")
    off_bytes = read_bytes(off_copy)
    off_text = off_bytes.decode("utf-8")
    mode_lines = [ln for ln in off_text.splitlines() if ln.strip().startswith("mode:")]
    print("写入 off 后的 mode 行:", mode_lines)
    quoted = any("off" in ln and ('"' in ln or "'" in ln) for ln in mode_lines)
    if not quoted:
        failures.append("approvals.mode: off 未被加引号（YAML 1.1 会读成 False）")

    _yaml_rt, doc = load_document(off_copy)
    approvals = doc.get("approvals") or {}
    value = approvals.get("mode")
    print(f"回读 approvals.mode = {value!r} (type={type(value).__name__})")
    if value != "off" or not isinstance(value, str):
        failures.append(f"回读值应为字符串 'off'，实际 {value!r}")

    safe = plain_yaml().load(off_text)
    safe_mode = (safe.get("approvals") or {}).get("mode")
    print(f"PyYAML(1.1) 读取 approvals.mode = {safe_mode!r}")
    if safe_mode != "off":
        failures.append(f"YAML 1.1 读取器得到 {safe_mode!r}，应为 'off'")

    print()
    if failures:
        print("失败项:")
        for item in failures:
            print(" -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
