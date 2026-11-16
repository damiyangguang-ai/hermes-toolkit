"""授权管理与证据存储。

设计原则来自《HFK改进》文档第一、四模块，并**刻意做得更严**：

  * 默认拒绝：没有授权记录 → 主动测试一律不启动；
  * 授权记录必须写全：目标、所有者、授权证明、起止时间、范围、禁止操作、停止条件；
  * 时间窗过期即失效；
  * 证据里的敏感值（密码、token、Cookie、Authorization）在落盘前脱敏；
  * 本模块只存"记录"，不判断请求本身是否合法——判断交给 :func:`assert_authorized`。

这里没有"扫别人网站"的开关，只有"证明你有权扫"的档案。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Authorization",
    "AuthorizationError",
    "AuthorizationStore",
    "Evidence",
    "EvidenceStore",
    "redact",
    "ACTIVE_SCOPES",
]

#: 允许的主动测试类型（被动项不需要授权）。
ACTIVE_SCOPES = ("sqli", "xss", "idor", "auth", "privesc", "ssrf")

#: 脱敏目标：键名命中即整值替换。
_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|session|credential)",
    re.I,
)


class AuthorizationError(Exception):
    """授权缺失、过期或范围不符。"""


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_time(value: str) -> _dt.datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


@dataclass
class Authorization:
    """一份授权记录。字段与文档第二模块逐条对应。"""

    id: str
    target: str                      # host 或 host:port
    owner: str                       # 资产所有者
    proof: str                       # 授权证明/记录（文件路径、工单号、邮件摘要）
    starts_at: str                   # ISO8601
    ends_at: str                     # ISO8601
    scopes: list[str] = field(default_factory=list)      # 允许的测试类型
    forbidden: list[str] = field(default_factory=list)   # 明确禁止的操作
    stop_conditions: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "owner": self.owner,
            "proof": self.proof,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "scopes": list(self.scopes),
            "forbidden": list(self.forbidden),
            "stop_conditions": list(self.stop_conditions),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Authorization":
        return cls(
            id=str(data["id"]),
            target=str(data["target"]).strip().lower(),
            owner=str(data["owner"]),
            proof=str(data.get("proof", "")),
            starts_at=str(data["starts_at"]),
            ends_at=str(data["ends_at"]),
            scopes=[str(item) for item in data.get("scopes") or []],
            forbidden=[str(item) for item in data.get("forbidden") or []],
            stop_conditions=[str(item) for item in data.get("stop_conditions") or []],
            note=str(data.get("note", "")),
        )

    # -- 校验 ---------------------------------------------------------------
    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.target:
            issues.append("缺少目标")
        if not self.owner:
            issues.append("缺少资产所有者")
        if not self.proof:
            issues.append("缺少授权证明")
        if not self.scopes:
            issues.append("缺少允许的测试范围")
        try:
            start = _parse_time(self.starts_at)
            end = _parse_time(self.ends_at)
            if end <= start:
                issues.append("结束时间必须晚于开始时间")
            now = _now()
            if now < start:
                issues.append(f"授权尚未生效（{self.starts_at} 开始）")
            if now > end:
                issues.append(f"授权已过期（{self.ends_at} 结束）")
        except ValueError as exc:
            issues.append(f"时间格式不合法：{exc}")
        for scope in self.scopes:
            if scope not in ACTIVE_SCOPES:
                issues.append(f"未知的测试类型：{scope}")
        return issues


class AuthorizationStore:
    """``authorizations.json`` 的读写。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[Authorization]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Authorization.from_dict(item) for item in data.get("authorizations") or []]

    def save(self, items: Iterable[Authorization]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "authorizations": [item.to_dict() for item in items]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def add(self, record: Authorization) -> None:
        items = self.load()
        if any(item.id == record.id for item in items):
            raise AuthorizationError(f"授权 id 已存在：{record.id}")
        items.append(record)
        self.save(items)

    def find_for(self, target: str, scope: str) -> Authorization | None:
        wanted = target.strip().lower()
        for item in self.load():
            if item.target == wanted and scope in item.scopes and not item.problems():
                return item
        return None

    # -- 闸门 ---------------------------------------------------------------
    def assert_authorized(self, target: str, scope: str) -> Authorization:
        """主动测试的唯一入口。不通过就抛错，绝不"先跑再说"。"""
        if scope not in ACTIVE_SCOPES:
            raise AuthorizationError(f"未知的测试类型：{scope}")
        record = self.find_for(target, scope)
        if record is not None:
            return record

        wanted = target.strip().lower()
        reasons: list[str] = []
        for item in self.load():
            if item.target != wanted:
                reasons.append(f"{item.target}：目标不匹配")
                continue
            problems = item.problems()
            if problems:
                reasons.append(f"{item.target}：{'；'.join(problems)}")
            else:
                reasons.append(f"{item.target}：授权范围不含 {scope}（当前 {'/'.join(item.scopes)}）")
        detail = "；".join(reasons) if reasons else "没有任何授权记录"
        raise AuthorizationError(
            f"拒绝执行主动测试：{target} 的 {scope} 缺少有效授权。现有记录 → {detail}"
        )


# --------------------------------------------------------------------------- 证据


def redact(value: Any) -> Any:
    """递归脱敏：命中敏感键名的值整体替换，字符串里的疑似凭据做正则替换。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key] = "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        # 常见凭据形态：Bearer xxx / password=xxx
        text = re.sub(r"(?i)(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}", r"\1 [REDACTED]", text)
        text = re.sub(r"(?i)\b(password|passwd|pwd|token|secret)=([^&\s]+)", r"\1=[REDACTED]", text)
        return text
    return value


@dataclass
class Evidence:
    """一条可复现的测试证据。"""

    evidence_id: str
    check: str
    target: str
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: str
    status: int | None
    response_headers: dict[str, str]
    response_body: str
    verdict: str            # vulnerable / not_vulnerable / inconclusive
    note: str = ""
    captured_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "evidence_id": self.evidence_id,
            "check": self.check,
            "target": self.target,
            "method": self.method,
            "url": self.url,
            "request_headers": redact(self.request_headers),
            "request_body": redact(self.request_body),
            "status": self.status,
            "response_headers": redact(self.response_headers),
            "response_body": redact(self.response_body),
            "verdict": self.verdict,
            "note": self.note,
            "captured_at": self.captured_at,
        }
        return payload


def new_evidence_id() -> str:
    return "ev_" + secrets.token_hex(6)


class EvidenceStore:
    """按项目保存证据（一条一个 JSON 文件，便于人工核对）。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def save(self, evidence: Evidence) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not evidence.captured_at:
            evidence.captured_at = _now().isoformat(timespec="seconds")
        path = self.directory / f"{evidence.evidence_id}.json"
        path.write_text(
            json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def load_all(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        items = []
        for path in sorted(self.directory.glob("ev_*.json")):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return items
