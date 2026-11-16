"""本地 HFK Core 服务。

为什么需要一个独立进程：Desktop 插件运行在 Electron renderer 沙箱里，
它**不能**安全地做「多文件写入 + 备份 + 事务 + 回滚」这类长事务。
所以插件只做 UI，真正的写入由本服务完成。

安全约束（逐条对应文档「本地服务必须」一节）：
  * 只监听 ``127.0.0.1``，绝不绑定 0.0.0.0；
  * 每次启动生成**随机令牌**，请求必须带 ``X-HFK-Token``；
  * 只暴露固定的动作集合，**拒绝任意命令执行**（没有 exec 端点）；
  * 校验 ``Host`` 头，抵御 DNS rebinding；
  * 令牌写入 ``<hfk>/service.json``（权限 0600）供本机插件读取，
    但**永不写入日志**、也**不在 HTTP 响应里回显**；
  * 所有操作写 ``<hfk>/logs/hfk.log``（不含令牌、不含秘密）。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import threading
from dataclasses import asdict  # noqa: F401  (保留：对外数据结构化时可能用到)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hfk.errors import HFKError
from hfk.facade import HFK
from hfk.util import ensure_dir, json_dump_atomic, utc_now_iso
from hfk.version import HFK_VERSION

__all__ = ["serve", "HFKService"]

_MAX_BODY = 1 << 20  # 1 MiB 足够：请求只包含字段路径与标量值


class _Service:
    """服务状态容器（令牌、HFK 会话、日志）。"""

    def __init__(self, hfk: HFK, token: str) -> None:
        self.hfk = hfk
        self.token = token
        self.log_path = hfk.env.hfk_dir / "logs" / "hfk.log"
        ensure_dir(self.log_path.parent)

    def log(self, message: str) -> None:
        line = f"{utc_now_iso()} {message}\n"
        with contextlib.suppress(OSError):
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        if os.environ.get("HFK_VERBOSE"):
            with contextlib.suppress(Exception):
                print(line.rstrip("\n"), file=sys.stderr)


def _restrict_to_current_user(path: Path) -> tuple[bool, str]:
    """把令牌文件的访问权限真正收紧到「当前用户」。

    为什么不能只用 ``os.chmod(path, 0o600)``：
      Windows 上 chmod 只改「只读」位，**不影响 ACL**。实测生成的
      service.json 继承了父目录的 ``(I)(F)``——SYSTEM / Administrators /
      Administrator 全部可读，等于没有保护。必须用 icacls 去掉继承。

    做法：``/inheritance:r`` 切断继承，``/grant:r <user>:(R,W)`` 只给当前用户。
    收紧后立刻回读验证；读不到就恢复继承，避免把用户自己锁在门外。
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True, "posix chmod 0600"
        except OSError as exc:
            return False, f"chmod 失败：{exc}"

    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return False, "拿不到当前用户名，跳过 ACL 收紧"
    try:
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"icacls 调用失败：{exc}"
    if completed.returncode != 0:
        return False, f"icacls 返回 {completed.returncode}：{(completed.stderr or '').strip()[:200]}"

    # 回读自检：确认自己还能读到，否则恢复继承
    try:
        path.read_bytes()
    except OSError as exc:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["icacls", str(path), "/inheritance:e"],
                capture_output=True,
                timeout=20,
                check=False,
            )
        return False, f"收紧后无法回读（{exc}），已恢复继承"
    return True, f"ACL 已收紧到 {user}"


class _QuietServer(ThreadingHTTPServer):
    """容忍浏览器/插件中途断开。

    Electron 页面刷新、标签页关闭、fetch 被 abort 都会直接断 TCP，
    socketserver 默认会把 ConnectionResetError 打到 stderr 上刷屏——
    对一个常驻本地服务来说这是噪声，不是错误。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return  # 客户端主动断开：忽略
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "HFKCore/" + HFK_VERSION
    protocol_version = "HTTP/1.1"

    # -- 基础设施 -----------------------------------------------------------
    @property
    def service(self) -> _Service:
        return self.server.hfk_service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 屏蔽默认的 stderr 访问日志（其中可能含 URL，但不含令牌；仍然统一走自己的日志）
        return

    def handle_one_request(self) -> None:
        """客户端断开连接时安静收场（见 :class:`_QuietServer`）。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        # 允许 file:// 页面（Electron renderer 的 Origin 常为 "null"）与本地页面
        allowed = {"null", "file://"}
        if origin in allowed or origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin or "null")
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-HFK-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # Chromium 的 Private Network Access 预检可能要求显式放行
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        """DNS rebinding 防护：Host 必须是本机回环。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _authorized(self) -> bool:
        provided = self.headers.get("X-HFK-Token", "")
        return bool(provided) and secrets.compare_digest(provided, self.service.token)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    # -- 路由 ---------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(403, {"error": {"code": "bad_host", "message": "只接受本机回环访问"}})
            return
        route = urlparse(self.path).path
        if route == "/api/health":
            self._send(200, {"ok": True, "hfk_version": HFK_VERSION})
            return
        if not self._authorized():
            self._send(401, {"error": {"code": "unauthorized", "message": "缺少或错误的 X-HFK-Token"}})
            return
        try:
            self._send(200, self._get(route))
        except HFKError as exc:
            self._send(400, {"error": exc.to_dict()})
        except Exception as exc:  # pragma: no cover
            self.service.log(f"GET {route} 失败: {exc}")
            self._send(500, {"error": {"code": "internal", "message": str(exc)}})

    def _get(self, route: str) -> Any:
        hfk = self.service.hfk
        table = {
            "/api/env": hfk.env_report,
            "/api/status": hfk.status,
            "/api/catalogue": hfk.catalogue,
            "/api/presets": hfk.presets,
            "/api/transactions": hfk.transactions,
            "/api/backups": hfk.backups,
            # 安全投影：只返回 HFK 管理字段，绝不回传整份 config.yaml
            "/api/config": hfk.managed_config,
        }
        handler = table.get(route)
        if handler is None:
            raise HFKError(f"未知路由：{route}")
        return handler()

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(403, {"error": {"code": "bad_host", "message": "只接受本机回环访问"}})
            return
        if not self._authorized():
            self._send(401, {"error": {"code": "unauthorized", "message": "缺少或错误的 X-HFK-Token"}})
            return
        route = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as exc:
            self._send(400, {"error": {"code": "bad_body", "message": str(exc)}})
            return
        try:
            self._send(200, self._post(route, body))
        except HFKError as exc:
            self.service.log(f"POST {route} 被拒绝: {exc.code} {exc.message}")
            self._send(400, {"error": exc.to_dict()})
        except Exception as exc:  # pragma: no cover
            self.service.log(f"POST {route} 失败: {exc}")
            self._send(500, {"error": {"code": "internal", "message": str(exc)}})

    def _post(self, route: str, body: dict[str, Any]) -> Any:
        hfk = self.service.hfk
        preset = body.get("preset")
        overrides = body.get("overrides") or {}
        items = body.get("items")

        if route == "/api/preview":
            return hfk.preview(preset=preset, overrides=overrides, items=items)
        if route == "/api/apply":
            result = hfk.apply(
                preset=preset,
                overrides=overrides,
                items=items,
                acknowledge_risk=bool(body.get("acknowledge_risk")),
            )
            self.service.log(
                f"apply 成功 transaction={result['transaction_id']} "
                f"fields={len(result.get('changes') or [])}"
            )
            return result
        if route == "/api/restore":
            result = hfk.restore(body.get("transaction_id"))
            self.service.log(
                f"restore 完成 status={result['status']} "
                f"restored={len(result['restored'])} conflicts={len(result['conflicts'])}"
            )
            return result
        if route == "/api/uninstall":
            result = hfk.uninstall(remove_plugin=bool(body.get("remove_plugin", True)))
            self.service.log("uninstall 完成")
            return result
        raise HFKError(f"未知路由：{route}")


class HFKService:
    """可嵌入的 HFK Core 服务（测试用得到）。"""

    def __init__(self, hfk: HFK, *, port: int = 0) -> None:
        self.token = secrets.token_urlsafe(32)
        self._state = _Service(hfk, self.token)
        self._server = _QuietServer(("127.0.0.1", port), _Handler)
        self._server.hfk_service = self._state  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._started: bool = False
        self._discovery_hardened: bool = False

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self._thread is not None:
            return  # 幂等：重复 start 不另起线程
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """停止服务。

        注意：``BaseServer.shutdown()`` 的文档明确写着「必须在 serve_forever()
        正在另一个线程里运行时调用，否则会死锁」。所以只有在真的 start 过之后
        才调用它——否则 stop() 会永久阻塞（这是被一个测试卡死 5 分钟测出来的）。
        """
        if self._started:
            with contextlib.suppress(Exception):
                self._server.shutdown()
            self._started = False
        with contextlib.suppress(Exception):
            self._server.server_close()

    # -- 令牌发现文件 -------------------------------------------------------
    def write_discovery_file(self) -> Path:
        hfk = self._state.hfk
        path = hfk.env.hfk_dir / "service.json"
        ensure_dir(path.parent)
        json_dump_atomic(
            path,
            {
                "pid": os.getpid(),
                "port": self.port,
                "base_url": self.base_url,
                "token": self.token,
                "hfk_version": HFK_VERSION,
                "started_at": utc_now_iso(),
            },
        )
        hardened, detail = _restrict_to_current_user(path)
        self._state.log(
            f"令牌文件 {path.name} 权限" + ("已收紧" if hardened else "未能收紧")
            + f"：{detail}"
        )
        self._discovery_hardened = hardened
        return path

    def remove_discovery_file(self) -> None:
        hfk = self._state.hfk
        with contextlib.suppress(OSError):
            (hfk.env.hfk_dir / "service.json").unlink()


def serve(port: int = 0, print_token: bool = False) -> int:
    hfk = HFK.connect()
    service = HFKService(hfk, port=port)
    discovery = service.write_discovery_file()
    print(f"HFK Core 服务已启动：{service.base_url}")
    print(f"令牌发现文件：{discovery}（权限 0600）")
    if print_token:
        print(f"HFK_TOKEN={service.token}")
    print("按 Ctrl+C 停止。仅监听 127.0.0.1，不暴露公网端口。")
    service.start()
    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        service.stop()
        service.remove_discovery_file()
    return 0
