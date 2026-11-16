"""本地靶场：一个**故意**有漏洞的小站，用于授权范围内的真实测试。

它不是模拟器——是一个真的 HTTP 服务，真的 SQLite，真的会返回别人的数据。
加 ``--fixed`` 会切到修复版，用于「修复前后对比」。

刻意保留的漏洞（都在 OWASP 范畴内）：
  1. SQL 注入（CWE-89）：``/login`` 直接拼接 SQL
  2. 越权/IDOR（CWE-639）：``/profile`` 不校验归属
  3. 安全响应头缺失（CWE-693）

只监听 127.0.0.1，防止被误用到真实网络。
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 修复版用于校验管理员身份的会话令牌（服务端权威，客户端无法通过改头伪造）
ADMIN_SESSIONS = {"sess-admin-7f3a"}

SEED_USERS = (
    (1, "alice", "pw-alice", "alice@example.test", "普通用户"),
    (2, "bob", "pw-bob", "bob@example.test", "普通用户"),
    (3, "admin", "pw-admin", "admin@example.test", "管理员"),
)

INDEX_HTML = """<!doctype html>
<html><head><title>Acme Portal</title></head>
<body><h1>Acme Portal</h1>
<form method="post" action="/login">
  <input name="username" placeholder="username">
  <input name="password" type="password" placeholder="password">
  <button>登录</button>
</form>
<p>Powered by AcmeApp v0.9.3</p>
</body></html>"""


def _build_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, email TEXT, role TEXT)"
    )
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?)", SEED_USERS)
    conn.commit()
    return conn


class _Handler(BaseHTTPRequestHandler):
    server_version = "AcmeApp/0.9.3"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    fixed = False
    db: sqlite3.Connection

    def log_message(self, fmt, *args):  # noqa: A002
        return  # 安静：证据由测试侧自己记录

    # -- 响应 ---------------------------------------------------------------
    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if self.fixed:
            # 修复版才会带的四个头
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, INDEX_HTML)
            return
        if parsed.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if parsed.path == "/profile":
            self._profile(parsed)
            return
        if parsed.path == "/search":
            self._search(parsed)
            return
        if parsed.path == "/fetch":
            self._fetch(parsed)
            return
        if parsed.path == "/admin":
            self._admin()
            return
        self._send(404, "not found")

    # -- 反射型 XSS ---------------------------------------------------------
    def _search(self, parsed) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        term = (query.get("q") or [""])[0]
        if self.fixed:
            body = f"<html><body><p>搜索结果：{html.escape(term)}</p></body></html>"
        else:
            # 漏洞：直接把用户输入拼进 HTML
            body = f"<html><body><p>搜索结果：{term}</p></body></html>"
        self._send(200, body)

    # -- SSRF ---------------------------------------------------------------
    def _fetch(self, parsed) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("url") or [""])[0]
        if not raw:
            self._json(400, {"error": "missing url"})
            return
        allowed = urllib.parse.urlparse(raw)
        if self.fixed:
            # 修复：只允许取本机 /healthz，其余一律拒绝
            if allowed.scheme != "http" or allowed.hostname not in ("127.0.0.1", "localhost"):
                self._json(403, {"error": "url not allowed"})
                return
            if allowed.path != "/healthz":
                self._json(403, {"error": "path not allowed"})
                return
        else:
            # 漏洞：服务端无条件代取任意 URL
            if allowed.scheme not in ("http", "https"):
                self._json(400, {"error": "unsupported scheme"})
                return
        try:
            with urllib.request.urlopen(raw, timeout=3) as response:  # noqa: S310
                text = response.read(4096).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": f"fetch failed: {exc}"})
            return
        self._json(200, {"fetched": text[:400]})

    # -- 信任客户端角色的越权 ------------------------------------------------
    def _admin(self) -> None:
        if self.fixed:
            # 修复：角色来自服务端会话，不信任客户端头
            session = self.headers.get("X-Session", "")
            if session not in ADMIN_SESSIONS:
                self._json(403, {"error": "forbidden"})
                return
        else:
            # 漏洞：直接相信客户端声明的角色
            if self.headers.get("X-Role", "") != "admin":
                self._json(403, {"error": "forbidden"})
                return
        self._json(200, {"admin": True, "panel": "用户管理"})

    def _profile(self, parsed) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        raw_id = (query.get("id") or ["1"])[0]
        if not raw_id.isdigit():
            self._json(400, {"error": "bad id"})
            return
        target_id = int(raw_id)

        if self.fixed:
            # 修复：必须带与目标一致的 X-User 头，否则 403
            caller = self.headers.get("X-User", "")
            cursor = self.db.execute("SELECT username FROM users WHERE id = ?", (target_id,))
            row = cursor.fetchone()
            if row is None or caller != row[0]:
                self._json(403, {"error": "forbidden"})
                return

        cursor = self.db.execute(
            "SELECT id, username, email, role FROM users WHERE id = ?", (target_id,)
        )
        row = cursor.fetchone()
        if row is None:
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"id": row[0], "username": row[1], "email": row[2], "role": row[3]})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/login":
            self._send(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw)
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]

        if self.fixed:
            # 修复：参数化查询
            cursor = self.db.execute(
                "SELECT id, username, role FROM users WHERE username = ? AND password = ?",
                (username, password),
            )
        else:
            # 漏洞：直接拼接
            sql = f"SELECT id, username, role FROM users WHERE username='{username}' AND password='{password}'"
            cursor = self.db.execute(sql)

        row = cursor.fetchone()
        if row is None:
            self._json(401, {"ok": False, "error": "invalid credentials"})
            return
        self._json(200, {"ok": True, "id": row[0], "username": row[1], "role": row[2]})


def create_server(port: int = 0, *, fixed: bool = False) -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    """启动靶场并返回 ``(server, port, thread)``，供测试在同一进程内使用。"""
    handler = type(
        "LabHandler",
        (_Handler,),
        {
            "fixed": fixed,
            "db": _build_db(),
            # 修复版连版本号都不再外泄（真实加固里常见的一步）。
            # 注意 sys_version 也要清空——否则 BaseHTTPRequestHandler 会自动拼上
            # "webserver Python/3.12.x"，照样泄露解释器版本。
            "server_version": "webserver" if fixed else "AcmeApp/0.9.3",
            "sys_version": "" if fixed else "",
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HFK 本地靶场（故意有漏洞，仅监听 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fixed", action="store_true", help="以修复版启动")
    args = parser.parse_args(argv)

    server, port, _ = create_server(args.port, fixed=args.fixed)
    mode = "修复版" if args.fixed else "漏洞版"
    print(f"靶场已启动（{mode}）：http://127.0.0.1:{port}")
    print("种子账号：alice/pw-alice  bob/pw-bob  admin/pw-admin")
    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        print("\n停止")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
