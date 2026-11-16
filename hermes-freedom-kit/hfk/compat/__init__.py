"""版本兼容系统。

第一版只承诺 Hermes **0.23.1**；同族 0.23.x 允许运行但会降级为「需要重新预览」的警告；
其它版本一律**只读**：可以查看历史事务、可以导出，但禁止 Apply。

判断依据是 :mod:`hfk.env` 解析出的版本三元组，而不是任何猜测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CompatResult", "load_manifests", "check", "manifest_for"]

_MANIFEST_DIR = Path(__file__).parent / "manifests"


@dataclass(frozen=True)
class CompatResult:
    """兼容性结论。"""

    supported: bool
    exact: bool
    version: str | None
    reason: str
    family: str | None = None
    manifest: dict | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "exact": self.exact,
            "version": self.version,
            "family": self.family,
            "reason": self.reason,
            "manifest_version": (self.manifest or {}).get("hermes_version"),
        }


def load_manifests() -> list[dict]:
    manifests: list[dict] = []
    if not _MANIFEST_DIR.is_dir():
        return manifests
    for path in sorted(_MANIFEST_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("hermes_version"):
            manifests.append(data)
    return manifests


def manifest_for(version: tuple[int, int, int] | None) -> dict | None:
    if version is None:
        return None
    target = ".".join(str(part) for part in version)
    for manifest in load_manifests():
        if str(manifest.get("hermes_version")) == target:
            return manifest
    return None


def check(version: tuple[int, int, int] | None, version_text: str | None = None) -> CompatResult:
    """判定该 Hermes 版本是否允许 Apply。"""
    display = version_text or (".".join(str(p) for p in version) if version else None)

    if version is None:
        return CompatResult(
            supported=False,
            exact=False,
            version=None,
            reason="无法确定 Hermes 版本，出于安全考虑禁止写入（只允许查看）",
        )

    manifest = manifest_for(version)
    if manifest is not None:
        return CompatResult(
            supported=True,
            exact=True,
            version=display,
            reason=f"精确匹配已验证版本 {display}",
            family=str(manifest.get("family") or ""),
            manifest=manifest,
        )

    # 同族（major.minor 相同）降级支持：字段可能仍存在，但必须重新预览并自行确认
    for candidate in load_manifests():
        family = str(candidate.get("family") or "")
        if family and family.endswith(".x"):
            prefix = family[:-2]
            if prefix == f"{version[0]}.{version[1]}":
                return CompatResult(
                    supported=True,
                    exact=False,
                    version=display,
                    reason=(
                        f"{display} 与已验证版本同属 {family} 分支，但未逐字段验证："
                        "允许运行，应用前必须重新预览并确认差异"
                    ),
                    family=family,
                    manifest=candidate,
                )

    known = ", ".join(str(item.get("hermes_version")) for item in load_manifests()) or "无"
    return CompatResult(
        supported=False,
        exact=False,
        version=display,
        reason=(
            f"未验证的 Hermes 版本 {display}（已支持：{known}）。"
            "为避免在未知版本上盲目套用旧补丁，HFK 只允许查看与导出历史事务，禁止 Apply"
        ),
    )
