"""HFK MCP Server —— 把 SecResearch 与 ExprDiag 暴露成 Hermes 可真实调用的工具。

接入方式（不改 Hermes 仓库任何文件）：

    hermes mcp add hfk --command <python.exe> --args -m hfkmcp \
        --env PYTHONPATH=<SecResearch 目录>

协议：MCP over stdio，JSON-RPC 2.0，每行一条消息。
只用标准库，方便在 Hermes 的 venv 里直接跑。

工具清单与安全边界：

  hfk_recon            被动侦察（无需授权，无侵入）
  hfk_lab_check        在**本机自建靶场**跑一次真实检查（回环地址，自授权）
  hfk_task_run         对**已授权**目标跑自主任务；未授权 → 只做被动
  hfk_task_status      查看任务账本
  hfk_authz_list       查看授权记录（只读）
  hfk_exprdiag         提示层只读诊断

**刻意不提供"创建授权记录"的工具**：授权必须由人在 CLI 里显式建立，
避免 agent 给自己开权限。这是整个体系的安全底线。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

SERVER_NAME = "hfk-secresearch"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# 让 secengine / lab 可导入（server.py 位于 SecResearch/hfkmcp/）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from secengine.checks import (  # noqa: E402
    check_admin_role,
    check_idor_profile,
    check_sqli_login,
    check_ssrf_fetch,
    check_xss_search,
)
from secengine.core import (  # noqa: E402
    Authorization,
    AuthorizationError,
    AuthorizationStore,
    EvidenceStore,
)
from secengine.recon import run_recon  # noqa: E402
from secengine.tasks import TaskEngine  # noqa: E402


# --------------------------------------------------------------------------- 工具实现


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _default_out() -> Path:
    base = os.environ.get("HFK_DATA_DIR")
    if base:
        return Path(base)
    return Path(tempfile.gettempdir()) / "hfk-mcp"


def tool_recon(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return _json({"error": "url 必须以 http:// 或 https:// 开头"})
    result = run_recon(url)
    if result.status is None:
        return _json({"error": f"请求失败：{result.error}", "url": url})
    return _json(
        {
            "url": url,
            "status": result.status,
            "server": result.headers.get("Server", "").strip(),
            "tech": result.tech,
            "tls": result.tls,
            "header_findings": result.findings,
            "note": "被动检查：单个 GET，无攻击载荷，不改变服务端状态",
        }
    )


_CHECKERS: dict[str, Callable[..., Any]] = {
    "sqli": check_sqli_login,
    "idor": check_idor_profile,
    "xss": check_xss_search,
    "ssrf": check_ssrf_fetch,
    "privesc": check_admin_role,
}


def tool_lab_check(args: dict[str, Any]) -> str:
    """在本机自建靶场跑一次真实检查（回环地址，无需第三方授权）。"""
    kind = str(args.get("kind") or "sqli").strip()
    checker = _CHECKERS.get(kind)
    if checker is None:
        return _json({"error": f"未知检查类型：{kind}，可选 {sorted(_CHECKERS)}"})

    from lab.vulnapp import create_server  # noqa: PLC0415

    out = _default_out() / "lab"
    evidence = EvidenceStore(out / "evidence")
    # 自建靶场属于自有资产：显式构造一条仅在本次调用内有效的授权记录
    record = Authorization(
        id="lab-inline",
        target="",  # 下面按真实端口回填
        owner="HFK 自建靶场（自有资产）",
        proof="lab/vulnapp.py 内置靶场，仅监听 127.0.0.1",
        starts_at="1970-01-01T00:00:00+00:00",
        ends_at="9999-12-31T00:00:00+00:00",
        scopes=list(_CHECKERS),
    )
    store = AuthorizationStore(out / "authz.json")

    server, port, _ = create_server(0, fixed=False)
    try:
        target = f"127.0.0.1:{port}"
        record.target = target
        record.id = f"lab-inline-{port}"
        # 每次调用都**覆盖**而不是追加：靶场端口每次不同，追加会让 id 冲突，
        # 第二次调用就会报「授权 id 已存在」（这是真实踩到的 bug）。
        store.save([record])
        _, finding = checker(store, target, f"http://{target}", evidence_store=evidence)
    finally:
        server.shutdown()
        server.server_close()

    return _json(
        {
            "check": finding.id,
            "verdict": finding.verdict,
            "cwe": finding.cwe,
            "severity": finding.severity,
            "target": target,
            "detail": finding.detail,
            "evidence_id": finding.evidence_id,
            "evidence_path": str(evidence.directory / f"{finding.evidence_id}.json"),
            "note": "在自建回环靶场上真实执行；未触碰任何第三方目标",
        }
    )


def tool_task_run(args: dict[str, Any]) -> str:
    target = str(args.get("target") or "").strip()
    base_url = str(args.get("base_url") or "").strip()
    if not target or not base_url:
        return _json({"error": "需要 target（host:port）与 base_url"})
    out = Path(str(args.get("out") or _default_out() / "task"))
    authz = AuthorizationStore(out / "authorizations.json")
    evidence = EvidenceStore(out / "evidence")
    engine = TaskEngine(authz, evidence, out)
    run = engine.plan(str(args.get("goal") or "对授权目标执行完整安全检查"), target, base_url)
    engine.run(run)
    report_path = out / "task-report.md"
    ok, problems = engine.verify(run, report_path)
    return _json(
        {
            "task_id": run.task_id,
            "status": run.status,
            "progress": run.progress,
            "planned_steps": [item.kind for item in run.steps],
            "verification_ok": ok,
            "verification": problems,
            "report": str(report_path),
            "ledger": str(engine.ledger_dir / f"{run.task_id}.json"),
            "note": "未授权的目标只会执行被动步骤——授权记录只能由人在 CLI 中建立",
        }
    )


def tool_task_status(args: dict[str, Any]) -> str:
    out = Path(str(args.get("out") or _default_out() / "task"))
    ledger = out / "tasks"
    items = []
    for path in sorted(ledger.glob("task-*.json")) if ledger.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "task_id": data.get("task_id"),
                "status": data.get("status"),
                "goal": data.get("goal"),
                "steps": f"{sum(1 for s in data.get('steps') or [] if s.get('status') in ('done','skipped'))}/{len(data.get('steps') or [])}",
            }
        )
    return _json({"count": len(items), "tasks": items})


def tool_authz_list(args: dict[str, Any]) -> str:
    out = Path(str(args.get("out") or _default_out() / "task"))
    store = AuthorizationStore(out / "authorizations.json")
    items = []
    for record in store.load():
        items.append(
            {
                "target": record.target,
                "owner": record.owner,
                "scopes": record.scopes,
                "valid": not record.problems(),
                "problems": record.problems(),
                "window": f"{record.starts_at} → {record.ends_at}",
            }
        )
    return _json(
        {
            "count": len(items),
            "authorizations": items,
            "note": "只读。新增授权请在终端运行 secengine authz add —— agent 不能给自己授权",
        }
    )


def tool_exprdiag(args: dict[str, Any]) -> str:
    # 延迟导入：ExprDiag 是另一个包，找不到时给明确错误而不是崩溃。
    # 目录名兼容 monorepo（exprdiag 小写）与独立仓库（ExprDiag）两种布局；
    # Linux 区分大小写，写死 "ExprDiag" 会让 CI 直接导入失败。
    candidates = [
        os.environ.get("EXPRDIAG_DIR", ""),
        str(_ROOT.parent / "exprdiag"),
        str(_ROOT.parent / "ExprDiag"),
    ]
    exprdiag_root = None
    for candidate in candidates:
        if candidate and (Path(candidate) / "exprdiag" / "inspect.py").is_file():
            exprdiag_root = Path(candidate)
            break
    if exprdiag_root is None:
        return _json(
            {"error": "找不到 exprdiag 包", "unsupported_environment": "exprdiag_missing"}
        )
    if str(exprdiag_root) not in sys.path:
        sys.path.insert(0, str(exprdiag_root))
    try:
        from exprdiag.inspect import diagnose, render_markdown  # noqa: PLC0415
    except ImportError as exc:
        return _json({"error": f"无法导入 exprdiag（{exc}）"})

    home = os.environ.get("LOCALAPPDATA")
    home_path = Path(home) / "hermes" if home else None
    if home_path is None or not home_path.exists():
        # 没有 Hermes 安装是**正常环境**（CI、其他机器），不是错误。
        # 明确标注，让调用方知道"没测"而不是"没限制"。
        return _json(
            {
                "available": False,
                "unsupported_environment": "no_hermes_home",
                "reason": "本机没有 Hermes Home（LOCALAPPDATA/hermes 不存在），无法做提示层诊断",
                "layers": 0,
                "conclusion": ["未执行诊断：环境中不存在 Hermes 安装"],
            }
        )
    install = home_path / "hermes-agent"
    report = diagnose(home_path, install if install.exists() else None)
    return _json(
        {
            "available": True,
            "layers": len(report.layers),
            "restriction_hits": len([i for i in report.findings if i.get("kind") == "restriction"]),
            "conflicts": report.conflicts,
            "conclusion": report.conclusion,
            "unknowns": report.unknowns,
            "markdown": render_markdown(report),
        }
    )


# --------------------------------------------------------------------------- 工具声明

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "hfk_recon",
        "description": (
            "对目标做**被动**安全侦察：HTTP 状态、Server 头、技术栈线索（带来源）、TLS 证书、"
            "安全响应头缺失。只发单个 GET，无攻击载荷，无需授权。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http(s):// 开头的目标地址"}},
            "required": ["url"],
        },
        "handler": tool_recon,
    },
    {
        "name": "hfk_lab_check",
        "description": (
            "在**本机自建回环靶场**上真实执行一次漏洞检查（sqli/idor/xss/ssrf/privesc），"
            "返回判定、CWE、证据文件路径。不触碰任何第三方目标，无需外部授权。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["sqli", "idor", "xss", "ssrf", "privesc"],
                    "description": "检查类型",
                }
            },
            "required": ["kind"],
        },
        "handler": tool_lab_check,
    },
    {
        "name": "hfk_task_run",
        "description": (
            "对**已授权**目标自主执行完整安全检查任务：自动拆解步骤、真实执行、写账本、"
            "生成报告并做完成校验（重新读磁盘，不看自述）。未授权目标只会执行被动步骤。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "host:port，必须与授权记录一致"},
                "base_url": {"type": "string", "description": "如 http://host:port"},
                "goal": {"type": "string", "description": "任务目标描述"},
                "out": {"type": "string", "description": "输出目录（可省略）"},
            },
            "required": ["target", "base_url"],
        },
        "handler": tool_task_run,
    },
    {
        "name": "hfk_task_status",
        "description": "查看已执行的安全任务账本（状态、进度）。",
        "inputSchema": {"type": "object", "properties": {"out": {"type": "string"}}},
        "handler": tool_task_status,
    },
    {
        "name": "hfk_authz_list",
        "description": (
            "列出授权记录及其有效性（只读）。新增授权必须由人在终端用 "
            "`secengine authz add` 建立——本工具刻意不提供创建授权的能力。"
        ),
        "inputSchema": {"type": "object", "properties": {"out": {"type": "string"}}},
        "handler": tool_authz_list,
    },
    {
        "name": "hfk_exprdiag",
        "description": (
            "只读诊断 Hermes 的提示层（SOUL.md / 系统提示词 / 插件）是否存在内容限制条款、"
            "跨层冲突，并给出拒绝归因证据。它回答「哪一层在拒」，不负责让被拒内容通过。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_exprdiag,
    },
]

_BY_NAME = {item["name"]: item for item in _TOOLS}


# --------------------------------------------------------------------------- JSON-RPC


def _log(message: str) -> None:
    """日志只走 stderr，绝不污染 stdout 的 JSON-RPC 流。"""
    sys.stderr.write(f"[hfk-mcp] {message}\n")
    sys.stderr.flush()


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method.startswith("notifications/"):
        return None  # 通知不需要响应

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": item["name"],
                        "description": item["description"],
                        "inputSchema": item["inputSchema"],
                    }
                    for item in _TOOLS
                ]
            },
        }

    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        tool = _BY_NAME.get(name)
        if tool is None:
            return _error(request_id, -32602, f"未知工具：{name}")
        try:
            text = tool["handler"](dict(arguments))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except AuthorizationError as exc:
            # 授权被拒是**设计行为**，作为结构化错误返回，不当成崩溃
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": _json({"error": str(exc), "refused": True})}],
                    "isError": True,
                },
            }
        except Exception as exc:  # noqa: BLE001
            _log("tool error: " + traceback.format_exc())
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": _json({"error": f"{type(exc).__name__}: {exc}"})}],
                    "isError": True,
                },
            }

    return _error(request_id, -32601, f"未知方法：{method}")


def main() -> int:
    _log(f"启动 {SERVER_NAME} {SERVER_VERSION}，root={_ROOT}")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(_error(None, -32700, "解析失败：不是合法 JSON"))
            continue
        if not isinstance(message, dict):
            _write(_error(None, -32600, "无效请求"))
            continue
        response = handle(message)
        if response is not None:
            _write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
