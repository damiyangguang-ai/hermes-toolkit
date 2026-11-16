"""被动侦察：只做**不产生侵入性影响**的检查。

文档第一模块的边界写得很清楚：未经授权时只允许被动公开信息收集。
所以这里的每一个函数都满足：单个 GET、不带攻击载荷、不改变服务端状态。

输出一律带 ``evidence``（看到了什么），而不是"我猜是 XX"。
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ReconResult", "fetch", "analyze_headers", "tls_info", "fingerprint", "run_recon"]

_UA = "HFK-SecEngine/0.1 (authorized-lab-only)"


@dataclass
class ReconResult:
    url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    tls: dict[str, Any] | None = None
    tech: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "headers": self.headers,
            "body_excerpt": self.body[:600],
            "error": self.error,
            "findings": self.findings,
            "tls": self.tls,
            "tech": self.tech,
        }


def fetch(url: str, *, timeout: float = 8.0, headers: dict[str, str] | None = None) -> tuple[int | None, dict[str, str], str, str]:
    """单次 GET。返回 ``(status, headers, body, error)``。"""
    request = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024)
            return (
                getattr(response, "status", None) or response.getcode(),
                {k: v for k, v in response.headers.items()},
                raw.decode("utf-8", "replace"),
                "",
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(64 * 1024).decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, {k: v for k, v in (exc.headers or {}).items()}, body, ""
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return None, {}, "", str(exc)


#: 期望存在的安全响应头（CWE-693 相关）。HSTS 只在 https 下要求。
EXPECTED_HEADERS = {
    "X-Content-Type-Options": "阻止 MIME 嗅探（nosniff）",
    "X-Frame-Options": "阻止被 iframe 嵌套（点击劫持）",
    "Content-Security-Policy": "限制脚本来源（XSS 缓解）",
    "Referrer-Policy": "限制 Referer 泄露",
}


def analyze_headers(headers: dict[str, str], *, is_https: bool) -> list[dict[str, Any]]:
    """检查缺失/过弱的安全响应头。只读响应头，安全无副作用。"""
    lowered = {key.lower(): value for key, value in headers.items()}
    findings: list[dict[str, Any]] = []
    for name, why in EXPECTED_HEADERS.items():
        if name.lower() not in lowered:
            findings.append(
                {
                    "id": f"missing-header:{name}",
                    "title": f"缺少安全响应头 {name}",
                    "cwe": "CWE-693",
                    "severity": "low",
                    "evidence": f"响应头中没有 {name}",
                    "impact": why,
                }
            )
    if is_https and "strict-transport-security" not in lowered:
        findings.append(
            {
                "id": "missing-header:Strict-Transport-Security",
                "title": "缺少 HSTS",
                "cwe": "CWE-319",
                "severity": "low",
                "evidence": "HTTPS 响应中没有 Strict-Transport-Security",
                "impact": "无法阻止降级到明文 HTTP",
            }
        )
    server = lowered.get("server", "")
    # 只有带版本号（含数字）的 Server 头才算信息泄露；
    # 单纯的 "webserver" 这种 banner 不构成可被利用的版本线索。
    if server and re.search(r"\d", server):
        findings.append(
            {
                "id": "info-disclosure:Server",
                "title": "Server 头暴露软件与版本",
                "cwe": "CWE-200",
                "severity": "info",
                "evidence": f"Server: {server}",
                "impact": "攻击者可据此匹配已知 CVE",
            }
        )
    return findings


def tls_info(host: str, port: int) -> dict[str, Any] | None:
    """读取 TLS 证书与协议版本（仅 https 有意义）。"""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                return {
                    "protocol": tls.version(),
                    "cipher": (tls.cipher() or ["?"])[0],
                    "subject": dict(x[0] for x in cert.get("subject", ())),
                    "issuer": dict(x[0] for x in cert.get("issuer", ())),
                    "not_after": cert.get("notAfter"),
                }
    except (ssl.SSLError, socket.error, OSError, ValueError):
        return None


#: 依据响应头/正文做技术栈识别。每条都带来源，不靠"感觉"。
_FINGERPRINTS: tuple[tuple[str, str, str], ...] = (
    (r"AcmeApp/([0-9.]+)", "AcmeApp", "Server 头"),
    (r"Powered by ([A-Za-z0-9 ._\-]{2,40})", "页面页脚", "HTML 正文"),
    (r"X-Powered-By:\s*([^\s]+)", "X-Powered-By", "响应头"),
    (r"wp-content/", "WordPress", "HTML 路径特征"),
    (r"__VIEWSTATE", "ASP.NET WebForms", "HTML 隐藏字段"),
    (r"JSESSIONID", "Java Servlet", "Cookie 名"),
)


def fingerprint(headers: dict[str, str], body: str) -> list[dict[str, str]]:
    """返回命中的技术栈线索（含版本，若有）。"""
    haystack = body + "\n" + "\n".join(f"{k}: {v}" for k, v in headers.items())
    hits: list[dict[str, str]] = []
    for pattern, label, source in _FINGERPRINTS:
        match = re.search(pattern, haystack, re.I)
        if match:
            version = match.group(1) if match.groups() else ""
            hits.append({"tech": label, "version": version, "source": source, "match": match.group(0)[:80]})
    return hits


def run_recon(url: str) -> ReconResult:
    """跑一次完整被动侦察。"""
    result = ReconResult(url=url)
    status, headers, body, error = fetch(url)
    result.status = status
    result.headers = headers
    result.body = body
    result.error = error
    if status is None:
        return result
    result.findings.extend(analyze_headers(headers, is_https=url.lower().startswith("https://")))
    result.tech = fingerprint(headers, body)
    if url.lower().startswith("https://"):
        host = re.sub(r"^https?://", "", url).split("/")[0]
        hostname, _, port_text = host.partition(":")
        result.tls = tls_info(hostname, int(port_text or 443))
    return result
