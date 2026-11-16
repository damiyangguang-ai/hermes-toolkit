"""真实 MCP 客户端测试：用子进程启动 server，走完整 JSON-RPC 握手并调用每个工具。

这不是 mock——它真的 spawn 一个 MCP server 进程、真的发 JSON-RPC、真的拿到响应。

用法：python -m hfkmcp.test_client
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MARK_OK = "\u2714"
MARK_FAIL = "\u2718"


class McpClient:
    def __init__(self, python: str, env_extra: dict[str, str] | None = None) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("HFK_DATA_DIR", None)
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            [python, "-m", "hfkmcp"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"server 没有响应，stderr={stderr[:2000]}")
        return json.loads(line)

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, name: str, arguments: dict | None = None) -> tuple[bool, dict]:
        response = self.send("tools/call", {"name": name, "arguments": arguments or {}})
        result = response.get("result") or {}
        content = result.get("content") or []
        text = content[0]["text"] if content else ""
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = {"raw": text}
        return bool(result.get("isError")), payload

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    # Windows 控制台默认 GBK，输出 ✔/✘ 会 UnicodeEncodeError。
    # 先尝试切 utf-8，失败则退化成纯 ASCII 标记。
    global MARK_OK, MARK_FAIL
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        MARK_OK, MARK_FAIL = "OK", "FAIL"

    python = sys.executable
    client = McpClient(python)
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok))
        mark = MARK_OK if ok else MARK_FAIL
        print(f"  {mark} {name}{'' if ok else '  ' + detail}")

    try:
        print("阶段 A｜握手")
        init = client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        result = init.get("result") or {}
        check("initialize 返回 serverInfo", bool(result.get("serverInfo")))
        check("声明 tools 能力", "tools" in (result.get("capabilities") or {}))
        client.notify("notifications/initialized")

        print("阶段 B｜工具清单")
        listing = client.send("tools/list")
        tools = (listing.get("result") or {}).get("tools") or []
        names = {item["name"] for item in tools}
        expected = {
            "hfk_recon",
            "hfk_lab_check",
            "hfk_task_run",
            "hfk_task_status",
            "hfk_authz_list",
            "hfk_exprdiag",
        }
        check(f"工具数量 {len(tools)}", expected.issubset(names), f"缺少 {expected - names}")
        for item in tools:
            check(f"  {item['name']} 有 inputSchema", isinstance(item.get("inputSchema"), dict))

        print("阶段 C｜真实调用")
        ok, payload = client.call("hfk_lab_check", {"kind": "sqli"})
        check("lab_check 检出 SQL 注入", (not ok) and payload.get("verdict") == "vulnerable", str(payload)[:200])
        check("lab_check 返回 CWE-89", payload.get("cwe") == "CWE-89", str(payload)[:200])
        evidence_path = Path(str(payload.get("evidence_path", "")))
        check("lab_check 证据文件真的存在", evidence_path.exists(), str(evidence_path))

        # 幂等性：连续两次调用不能因授权记录累积而失败（真实踩过的 bug）
        ok2, payload2 = client.call("hfk_lab_check", {"kind": "idor"})
        check("lab_check 第二次调用仍成功", (not ok2) and payload2.get("verdict") == "vulnerable", str(payload2)[:200])

        ok, payload = client.call("hfk_authz_list", {})
        check("authz_list 可读", (not ok) and "authorizations" in payload, str(payload)[:200])

        ok, payload = client.call("hfk_exprdiag", {})
        check("exprdiag 返回层数", (not ok) and payload.get("layers", 0) > 0, str(payload)[:200])
        check("exprdiag 结论非空", bool(payload.get("conclusion")), str(payload)[:200])

        # 未授权目标：任务必须只排被动步骤（授权闸门不可被 agent 绕过）
        ok, payload = client.call(
            "hfk_task_run", {"target": "127.0.0.1:1", "base_url": "http://127.0.0.1:1", "out": str(Path(os.environ.get("TEMP", ".")) / "hfk-mcp-unauth")}
        )
        steps = payload.get("planned_steps") or []
        check("未授权目标不排主动步骤", "sqli" not in steps, f"steps={steps}")
        check("未授权任务仍交付被动结论", payload.get("status") in ("completed", "failed"), str(payload)[:200])

        print("阶段 D｜错误处理")
        response = client.send("tools/call", {"name": "no_such_tool", "arguments": {}})
        check("未知工具返回 JSON-RPC 错误", "error" in response)
        response = client.send("no/such/method")
        check("未知方法返回 JSON-RPC 错误", "error" in response)
    finally:
        client.close()

    failed = [name for name, ok in checks if not ok]
    print()
    if failed:
        print(f"失败 {len(failed)} 项：")
        for item in failed:
            print(f"  · {item}")
        return 1
    print(f"全部通过：{len(checks)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
