"""HFK Security Research Engine。

定位：**授权范围内**的安全研究。默认拒绝——没有授权记录就不跑主动测试。
"""

from .checks import (
    Finding,
    check_admin_role,
    check_headers,
    check_idor_profile,
    check_sqli_login,
    check_ssrf_fetch,
    check_xss_search,
)
from .core import (
    ACTIVE_SCOPES,
    Authorization,
    AuthorizationError,
    AuthorizationStore,
    Evidence,
    EvidenceStore,
    redact,
)
from .recon import analyze_headers, fetch, run_recon, tls_info
from .report import build_report

__all__ = [
    "ACTIVE_SCOPES",
    "Authorization",
    "AuthorizationError",
    "AuthorizationStore",
    "Evidence",
    "EvidenceStore",
    "Finding",
    "analyze_headers",
    "build_report",
    "check_admin_role",
    "check_headers",
    "check_idor_profile",
    "check_sqli_login",
    "check_ssrf_fetch",
    "check_xss_search",
    "fetch",
    "redact",
    "run_recon",
    "tls_info",
]

__version__ = "0.1.0"
