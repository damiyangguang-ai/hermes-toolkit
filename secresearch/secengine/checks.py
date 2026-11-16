"""主动测试：只有拿到有效授权才会执行。

与 :mod:`secengine.recon` 的分界线：

  * 被动（响应头、技术栈、TLS）→ 不需要授权，因为不产生侵入性影响；
  * 主动（注入载荷、越权访问）→ **必须**先通过 :meth:`AuthorizationStore.assert_authorized`。

每个主动检查都返回 ``(Evidence, Finding)``：Evidence 是可复现的请求/响应，
Finding 是可写进报告的结论。两者都带 ``verdict``，且允许 ``inconclusive``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .core import (
    AuthorizationStore,
    Evidence,
    EvidenceStore,
    new_evidence_id,
    redact,
)
from .recon import analyze_headers, fetch

__all__ = [
    "Finding",
    "check_sqli_login",
    "check_idor_profile",
    "check_headers",
    "check_xss_search",
    "check_ssrf_fetch",
    "check_admin_role",
]

_UA = "HFK-SecEngine/0.1 (authorized-lab-only)"


@dataclass
class Finding:
    id: str
    title: str
    cwe: str
    severity: str
    verdict: str            # vulnerable / not_vulnerable / inconclusive
    impact: str
    fix: str
    evidence_id: str = ""
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "cwe": self.cwe,
            "severity": self.severity,
            "verdict": self.verdict,
            "impact": self.impact,
            "fix": self.fix,
            "evidence_id": self.evidence_id,
            "detail": list(self.detail),
        }


def _post_form(url: str, fields: dict[str, str], *, timeout: float = 8.0) -> tuple[int | None, dict[str, str], str, str]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(64 * 1024).decode("utf-8", "replace")
            return response.getcode(), dict(response.headers), text, ""
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(64 * 1024).decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, dict(exc.headers or {}), text, ""
    except (urllib.error.URLError, OSError) as exc:
        return None, {}, "", str(exc)


def _save(store: EvidenceStore | None, evidence: Evidence) -> str:
    if store is not None:
        store.save(evidence)
    return evidence.evidence_id


# --------------------------------------------------------------------------- SQLi


SQLI_PAYLOAD = "' OR '1'='1' -- "
SQLI_WRONG_PASSWORD = "definitely-not-the-password"


def check_sqli_login(
    authz: AuthorizationStore,
    target: str,
    base_url: str,
    *,
    evidence_store: EvidenceStore | None = None,
) -> tuple[Evidence, Finding]:
    """用认证绕过载荷验证 ``/login`` 是否存在 SQL 注入（CWE-89）。

    判据：用一个**错误的密码**登录，如果仍然成功，说明密码条件被注释掉了——
    这是注入成立的行为学证据，不是"看响应像不像报错"。
    """
    authz.assert_authorized(target, "sqli")  # ← 闸门：无授权直接抛错

    url = base_url.rstrip("/") + "/login"
    fields = {"username": SQLI_PAYLOAD, "password": SQLI_WRONG_PASSWORD}
    status, headers, body, error = _post_form(url, fields)

    # 判据必须解析 JSON，不能靠字符串里有没有空格之类的巧合。
    # （第一版写成 body.replace(" ", "") 里找 '"ok": true'，自己把空格去了又带空格比对，
    #  结果永远判定 not_vulnerable —— 漏报比误报更危险，已改为结构化解析。）
    payload: dict | None = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            payload = parsed
    except (ValueError, TypeError):
        payload = None

    vulnerable = status == 200 and payload is not None and payload.get("ok") is True
    verdict = "vulnerable" if vulnerable else ("inconclusive" if status is None else "not_vulnerable")

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="sqli-login",
        target=target,
        method="POST",
        url=url,
        request_headers={"Content-Type": "application/x-www-form-urlencoded"},
        request_body=f"username={SQLI_PAYLOAD}&password={SQLI_WRONG_PASSWORD}",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:2000],
        verdict=verdict,
        note=error or ("错误密码仍登录成功 → 密码校验被注入注释掉" if vulnerable else "未绕过"),
    )
    evidence_id = _save(evidence_store, evidence)

    return evidence, Finding(
        id="sqli-login",
        title="登录接口 SQL 注入（认证绕过）",
        cwe="CWE-89",
        severity="high",
        verdict=verdict,
        impact="无需正确密码即可登录任意账号；可进一步读取整库数据",
        fix="使用参数化查询（占位符 + 绑定参数），禁止字符串拼接 SQL",
        evidence_id=evidence_id,
        detail=[
            f"请求：POST {url}",
            f"载荷：username={SQLI_PAYLOAD!r}（密码使用错误值）",
            f"响应状态：{status}",
        ],
    )


# --------------------------------------------------------------------------- IDOR


def check_idor_profile(
    authz: AuthorizationStore,
    target: str,
    base_url: str,
    *,
    victim_id: int = 2,
    evidence_store: EvidenceStore | None = None,
) -> tuple[Evidence, Finding]:
    """验证 ``/profile?id=N`` 是否缺少归属校验（CWE-639）。"""
    authz.assert_authorized(target, "idor")  # ← 闸门

    url = f"{base_url.rstrip('/')}/profile?id={victim_id}"
    status, headers, body, error = fetch(url)

    leaked = status == 200 and '"username"' in body
    verdict = "vulnerable" if leaked else ("inconclusive" if status is None else "not_vulnerable")

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="idor-profile",
        target=target,
        method="GET",
        url=url,
        request_headers={"User-Agent": _UA},
        request_body="",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:2000],
        verdict=verdict,
        note=error or ("未携带任何身份信息却读到了 id=" + str(victim_id) + " 的资料" if leaked else "被拒绝"),
    )
    evidence_id = _save(evidence_store, evidence)

    return evidence, Finding(
        id="idor-profile",
        title="用户资料越权访问（IDOR）",
        cwe="CWE-639",
        severity="medium",
        verdict=verdict,
        impact="未登录即可遍历 id 读取他人邮箱与角色",
        fix="按会话身份校验资源归属，禁止仅凭请求参数定位资源",
        evidence_id=evidence_id,
        detail=[
            f"请求：GET {url}（无 Cookie / 无身份头）",
            f"响应状态：{status}",
        ],
    )


# --------------------------------------------------------------------------- 反射型 XSS


XSS_PAYLOAD = "<script>alert(1)</script>"


def check_xss_search(
    authz: AuthorizationStore,
    target: str,
    base_url: str,
    *,
    evidence_store: EvidenceStore | None = None,
) -> tuple[Evidence, Finding]:
    """验证 ``/search?q=`` 是否把输入原样反射进 HTML（CWE-79）。

    判据：响应正文里出现**未转义**的原始载荷；若被转成 ``&lt;script&gt;``
    则视为已正确编码。
    """
    authz.assert_authorized(target, "xss")

    url = f"{base_url.rstrip('/')}/search?q={urllib.parse.quote(XSS_PAYLOAD)}"
    status, headers, body, error = fetch(url)

    reflected_raw = XSS_PAYLOAD in body
    escaped = "&lt;script&gt;" in body
    vulnerable = status == 200 and reflected_raw and not escaped
    verdict = "vulnerable" if vulnerable else ("inconclusive" if status is None else "not_vulnerable")

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="xss-search",
        target=target,
        method="GET",
        url=url,
        request_headers={"User-Agent": _UA},
        request_body="",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:2000],
        verdict=verdict,
        note=error
        or ("载荷被原样写入 HTTP 响应" if vulnerable else ("已做 HTML 转义" if escaped else "未反射")),
    )
    evidence_id = _save(evidence_store, evidence)

    return evidence, Finding(
        id="xss-search",
        title="搜索接口反射型 XSS",
        cwe="CWE-79",
        severity="medium",
        verdict=verdict,
        impact="可注入脚本，窃取会话或冒充用户操作",
        fix="输出到 HTML 上下文时做实体编码；配合 CSP 作为纵深防御",
        evidence_id=evidence_id,
        detail=[f"请求：GET {url}", f"响应状态：{status}"],
    )


# --------------------------------------------------------------------------- SSRF


def check_ssrf_fetch(
    authz: AuthorizationStore,
    target: str,
    base_url: str,
    *,
    evidence_store: EvidenceStore | None = None,
) -> tuple[Evidence, Finding]:
    """验证 ``/fetch?url=`` 是否让服务端代取**用户指定**的地址（CWE-918）。

    判据：传入一个**修复后本不该被允许**的内部地址（靶场自己的
    ``/profile?id=2``），若服务端真的取回并回显，说明 URL 完全由请求方控制。

    注意不能用 ``/healthz`` 做证据：它本身就在修复版的白名单里，
    那样会把"修复生效"误判成"仍然可被 SSRF"（第一版就踩了这个坑）。
    """
    authz.assert_authorized(target, "ssrf")

    inner = f"{base_url.rstrip('/')}/profile?id=2"
    url = f"{base_url.rstrip('/')}/fetch?url={urllib.parse.quote(inner, safe='')}"
    status, headers, body, error = fetch(url)

    served = status == 200 and '"fetched"' in body
    verdict = "vulnerable" if served else ("inconclusive" if status is None else "not_vulnerable")

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="ssrf-fetch",
        target=target,
        method="GET",
        url=url,
        request_headers={"User-Agent": _UA},
        request_body="",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:2000],
        verdict=verdict,
        note=error
        or (
            "服务端接受了请求方指定的内部地址并回显取回内容"
            if served
            else "拒绝代取白名单之外的地址"
        ),
    )
    evidence_id = _save(evidence_store, evidence)

    return evidence, Finding(
        id="ssrf-fetch",
        title="URL 代取接口 SSRF",
        cwe="CWE-918",
        severity="medium",
        verdict=verdict,
        impact="可借服务端身份访问内网/元数据地址，绕过网络边界",
        fix="对目标地址做协议+主机+端口白名单，禁止跟随重定向到非白名单地址",
        evidence_id=evidence_id,
        detail=[f"请求：GET {url}", f"内层被取地址：{inner}", f"响应状态：{status}"],
    )


# --------------------------------------------------------------------------- 信任客户端角色


def check_admin_role(
    authz: AuthorizationStore,
    target: str,
    base_url: str,
    *,
    evidence_store: EvidenceStore | None = None,
) -> tuple[Evidence, Finding]:
    """验证 ``/admin`` 是否**相信客户端声明的角色**（CWE-285）。

    判据：不带任何有效会话，只把一个 ``X-Role: admin`` 头塞进去。
    若返回 200，说明权限判断建立在请求方可伪造的信息上。
    """
    authz.assert_authorized(target, "privesc")

    url = f"{base_url.rstrip('/')}/admin"
    status, headers, body, error = fetch(url, headers={"X-Role": "admin"})

    # 结构化判定，不做字符串空格游戏（同类漏报已经踩过一次）
    payload: dict | None = None
    try:
        parsed_body = json.loads(body)
        if isinstance(parsed_body, dict):
            payload = parsed_body
    except (ValueError, TypeError):
        payload = None
    bypassed = status == 200 and payload is not None and payload.get("admin") is True
    verdict = "vulnerable" if bypassed else ("inconclusive" if status is None else "not_vulnerable")

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="auth-admin-role",
        target=target,
        method="GET",
        url=url,
        request_headers={"User-Agent": _UA, "X-Role": "admin"},
        request_body="",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:2000],
        verdict=verdict,
        note=error
        or ("伪造角色头即获得管理员面板" if bypassed else "角色校验不依赖客户端头"),
    )
    evidence_id = _save(evidence_store, evidence)

    return evidence, Finding(
        id="auth-admin-role",
        title="信任客户端角色头的越权",
        cwe="CWE-285",
        severity="high",
        verdict=verdict,
        impact="任何请求方改一个头即可获得管理员权限",
        fix="角色必须来自服务端会话/令牌，禁止以客户端提交的角色字段作为授权依据",
        evidence_id=evidence_id,
        detail=[f"请求：GET {url}（Header: X-Role: admin，无有效会话）", f"响应状态：{status}"],
    )


# --------------------------------------------------------------------------- 被动


def check_headers(base_url: str, *, evidence_store: EvidenceStore | None = None) -> tuple[Evidence, list[Finding]]:
    """安全响应头检查。**属被动项，不需要授权**。"""
    status, headers, body, error = fetch(base_url)
    findings_out: list[Finding] = []
    header_findings = analyze_headers(headers, is_https=base_url.lower().startswith("https://"))

    evidence = Evidence(
        evidence_id=new_evidence_id(),
        check="security-headers",
        target=urllib.parse.urlparse(base_url).netloc,
        method="GET",
        url=base_url,
        request_headers={"User-Agent": _UA},
        request_body="",
        status=status,
        response_headers={k: v for k, v in headers.items()},
        response_body=body[:600],
        verdict="vulnerable" if header_findings else "not_vulnerable",
        note=error or f"发现 {len(header_findings)} 项响应头问题",
    )
    evidence_id = _save(evidence_store, evidence)

    for item in header_findings:
        findings_out.append(
            Finding(
                id=str(item["id"]),
                title=str(item["title"]),
                cwe=str(item["cwe"]),
                severity=str(item["severity"]),
                verdict="vulnerable",
                impact=str(item["impact"]),
                fix="按响应头语义补齐配置（注意 CSP 需要按站点实际资源来源调整）",
                evidence_id=evidence_id,
                detail=[str(item["evidence"])],
            )
        )
    return evidence, findings_out
