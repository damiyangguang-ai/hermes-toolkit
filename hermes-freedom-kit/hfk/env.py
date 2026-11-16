"""宿主环境识别（Host Scanner）。

回答文档要求的那一组问题：Hermes 装在哪、是哪个版本、Home 在哪、当前 Profile 是哪个、
Gateway 是否在跑、配置能不能读、有没有未完成的 HFK 事务。

**版本识别的一条铁律**：绝不把 ``apps/desktop/package.json`` 的版本号当成 Hermes Agent 版本。
本机实测该文件写的是 ``0.0.0``——正是文档警告的陷阱。权威来源按优先级为：

1. ``install-stamp.json``（安装器写入：baseVersion / displayVersion / commit / dirty）
2. 安装目录的 git 标签（``git describe --tags``）
3. ``gateway_state.json`` 里的 ``code_version``（运行中的网关自报）

三者互相印证；不一致时在 ``warnings`` 里说明，而不是悄悄选一个。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hfk.errors import ConfigReadError, EnvironmentNotFoundError
from hfk.util import sha256_file

__all__ = [
    "GatewayStatus",
    "HermesEnvironment",
    "detect",
    "find_hermes_home",
    "find_install_dir",
    "parse_version",
]

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


# --------------------------------------------------------------------------- 小工具


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """把 ``v0.23.1`` / ``0.23.1-3-gabc`` 解析成 ``(0, 23, 1)``；无法解析返回 ``None``。"""
    if not text:
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存在。

    Windows 上**不能**用 ``os.kill(pid, 0)``：在 Windows 上 ``os.kill`` 会真的发信号、
    可能直接终止目标进程。这里用 ``OpenProcess`` + ``GetExitCodeProcess``。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run(argv: list[str], *, cwd: Path | None = None, timeout: float = 8.0) -> str | None:
    """有界子进程调用；失败一律返回 ``None``（环境探测不允许因外部命令失败而崩）。"""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


# --------------------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class GatewayStatus:
    """本机 Hermes Gateway 状态。"""

    running: bool = False
    pid: int | None = None
    state: str | None = None
    code_version: str | None = None
    active_agents: int | None = None
    served_profiles: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "pid": self.pid,
            "state": self.state,
            "code_version": self.code_version,
            "active_agents": self.active_agents,
            "served_profiles": list(self.served_profiles),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HermesEnvironment:
    """一次完整的环境快照。"""

    home: Path
    config_path: Path
    hfk_dir: Path
    profile: str
    install_dir: Path | None = None
    version: str | None = None
    version_tuple: tuple[int, int, int] | None = None
    version_source: str = "unknown"
    commit: str | None = None
    git_tag: str | None = None
    git_dirty: bool | None = None
    gateway: GatewayStatus = field(default_factory=GatewayStatus)
    python_version: str = field(default_factory=platform.python_version)
    node_version: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    config_readable: bool = False
    config_error: str | None = None
    config_sha256: str | None = None
    config_bytes: int | None = None
    desktop_package_version: str | None = None
    pending_transactions: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """给 CLI / HTTP API 用的可序列化视图。"""
        return {
            "home": str(self.home),
            "config_path": str(self.config_path),
            "hfk_dir": str(self.hfk_dir),
            "profile": self.profile,
            "install_dir": str(self.install_dir) if self.install_dir else None,
            "version": self.version,
            "version_source": self.version_source,
            "commit": self.commit,
            "git_tag": self.git_tag,
            "git_dirty": self.git_dirty,
            "gateway": self.gateway.to_dict(),
            "python_version": self.python_version,
            "node_version": self.node_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "config_readable": self.config_readable,
            "config_error": self.config_error,
            "config_sha256": self.config_sha256,
            "config_bytes": self.config_bytes,
            "desktop_package_version": self.desktop_package_version,
            "pending_transactions": self.pending_transactions,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- 定位


def find_hermes_home(explicit: str | Path | None = None) -> Path:
    """按 Hermes 自身的解析顺序定位 Hermes Home。

    顺序：显式指定 → ``HERMES_HOME`` → 平台默认（Windows: ``%LOCALAPPDATA%\\hermes``，
    支持 ``HERMES_DATA_DIR_SUFFIX``）→ ``~/.hermes``。

    与文档里写的 ``%USERPROFILE%\\.hermes`` 不同：那是 POSIX 默认值；
    Hermes 在 Windows 上真正的默认是 ``%LOCALAPPDATA%\\hermes``（本机实测如此）。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(os.path.expandvars(str(explicit))).expanduser())
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        candidates.append(Path(os.path.expandvars(env_home)).expanduser())
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        candidates.append(base / ("hermes" + suffix))
    candidates.append(Path.home() / (".hermes" + suffix))

    markers = ("config.yaml", ".env", "state.db")
    for candidate in candidates:
        try:
            if candidate.is_dir() and any((candidate / marker).exists() for marker in markers):
                return candidate
        except OSError:
            continue
    # 退化：目录存在但没有任何标记文件时，只要显式指定就接受（用户可能刚装上还没生成配置）
    if explicit and candidates[0].is_dir():
        return candidates[0]
    raise EnvironmentNotFoundError(
        "没有找到 Hermes Home（尝试过 %LOCALAPPDATA%\\hermes 与 ~/.hermes）。"
        "请确认 Hermes 已安装，或用 --home 明确指定。",
        detail="; ".join(str(item) for item in candidates),
    )


def _home_from_gateway_pid(home: Path) -> Path | None:
    """从网关 pid 文件反推安装目录归属的 Home（用于校验探测结果）。"""
    data = _read_json(home / "gateway.pid")
    if not data:
        return None
    recorded = data.get("hermes_home")
    return Path(recorded) if recorded else None


def find_install_dir(home: Path, explicit: str | Path | None = None) -> Path | None:
    """定位 Hermes 安装目录（源码/发行版根）。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(os.path.expandvars(str(explicit))).expanduser())
    env_dir = os.environ.get("HERMES_AGENT_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    # 1) 网关命令行里第一个 .py 的父父目录（最贴近「正在跑的这套代码」）
    pid_data = _read_json(home / "gateway.pid")
    if pid_data:
        argv = pid_data.get("argv") or []
        for item in argv:
            if isinstance(item, str) and item.endswith(".py"):
                candidates.append(Path(item).resolve().parent.parent)
                break

    # 2) 平台默认安装位置
    candidates.append(home / "hermes-agent")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            candidates.append(Path(local) / "hermes" / "hermes-agent")

    for candidate in candidates:
        try:
            if candidate.is_dir() and (candidate / "hermes_cli").is_dir():
                return candidate
        except OSError:
            continue
    return None


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- 版本


def _read_install_stamp(install_dir: Path | None) -> dict | None:
    if install_dir is None:
        return None
    return _read_json(install_dir / "install-stamp.json")


def _git_info(install_dir: Path | None) -> dict[str, object]:
    """读 git 标签 / HEAD / 是否脏。安装目录不是 git 仓库时全部返回 None。"""
    result: dict[str, object] = {"tag": None, "head": None, "dirty": None}
    if install_dir is None or not (install_dir / ".git").exists():
        return result
    result["tag"] = _run(["git", "describe", "--tags", "--always"], cwd=install_dir)
    result["head"] = _run(["git", "rev-parse", "HEAD"], cwd=install_dir)
    status = _run(["git", "status", "--porcelain"], cwd=install_dir)
    result["dirty"] = bool(status) if status is not None else None
    return result


def _desktop_package_version(install_dir: Path | None) -> str | None:
    """仅供「证明我们没用它」而读取。"""
    if install_dir is None:
        return None
    data = _read_json(install_dir / "apps" / "desktop" / "package.json")
    if not data:
        return None
    version = data.get("version")
    return str(version) if version is not None else None


def _gateway_status(home: Path) -> GatewayStatus:
    pid_data = _read_json(home / "gateway.pid") or {}
    state_data = _read_json(home / "gateway_state.json") or {}
    pid_raw = pid_data.get("pid") or state_data.get("pid")
    pid = int(pid_raw) if isinstance(pid_raw, (int, str)) and str(pid_raw).isdigit() else None

    if pid is None:
        return GatewayStatus(running=False, detail="未找到 gateway.pid")

    alive = _pid_alive(pid)
    state = state_data.get("gateway_state") or pid_data.get("kind")
    served = state_data.get("served_profiles") or ()
    active = state_data.get("active_agents")
    code_version = state_data.get("code_version")
    if not alive:
        detail = f"gateway.pid 记录了 pid {pid}，但该进程已不在运行（可能上次异常退出）"
    else:
        detail = f"gateway pid {pid} 正在运行，状态 {state}"
    return GatewayStatus(
        running=alive,
        pid=pid,
        state=str(state) if state else None,
        code_version=str(code_version) if code_version else None,
        active_agents=active if isinstance(active, int) else None,
        served_profiles=tuple(served) if isinstance(served, (list, tuple)) else (),
        detail=detail,
    )


def _profile_for_home(home: Path) -> str:
    """``<root>/profiles/<name>`` → ``name``；其余情况为 ``default``。"""
    if home.parent.name == "profiles" and not home.name.startswith("."):
        return home.name
    return "default"


# --------------------------------------------------------------------------- 主入口


def detect(
    home: str | Path | None = None,
    install_dir: str | Path | None = None,
) -> HermesEnvironment:
    """采集一次完整环境快照。

    任何「读不出来」的情况都不会抛异常打断流程，而是记录在返回值的字段/warnings 里，
    由上层决定是否拒绝 Apply（配置不可读时**必须**拒绝）。
    """
    warnings: list[str] = []
    resolved_home = find_hermes_home(home)
    config_path = resolved_home / "config.yaml"
    hfk_dir = resolved_home / "hfk"

    recorded_home = _home_from_gateway_pid(resolved_home)
    if recorded_home is not None:
        try:
            if recorded_home.resolve(strict=False) != resolved_home.resolve(strict=False):
                warnings.append(
                    f"网关记录的 hermes_home（{recorded_home}）与探测结果（{resolved_home}）不一致"
                )
        except OSError:
            pass

    resolved_install = find_install_dir(resolved_home, install_dir)
    stamp = _read_install_stamp(resolved_install)
    git = _git_info(resolved_install)
    gateway = _gateway_status(resolved_home)

    # ---- 版本：install-stamp → git tag → gateway 自报
    version: str | None = None
    version_source = "unknown"
    commit: str | None = None
    git_dirty: bool | None = None
    if stamp:
        version = stamp.get("displayVersion") or stamp.get("baseVersion")
        if version:
            version_source = "install-stamp.json"
        commit = stamp.get("commit")
        git_dirty = stamp.get("dirty") if isinstance(stamp.get("dirty"), bool) else None
    if not version and git.get("tag"):
        version = str(git["tag"])
        version_source = "git describe"
    if not version and gateway.code_version:
        version = gateway.code_version
        version_source = "gateway_state.json"
    if not version:
        warnings.append("无法确定 Hermes 版本（install-stamp.json / git / gateway 均不可用）")

    if commit and git.get("head") and commit != git.get("head"):
        warnings.append(
            f"install-stamp 记录的 commit（{str(commit)[:12]}）与当前 HEAD（{str(git['head'])[:12]}）不一致："
            "安装后代码被更新过，兼容性需重新判定"
        )
    if git_dirty is None and git.get("dirty") is not None:
        git_dirty = bool(git["dirty"])
    if git_dirty:
        warnings.append("安装目录存在未提交的本地修改（git dirty）")

    desktop_version = _desktop_package_version(resolved_install)
    if desktop_version is not None and parse_version(desktop_version) == (0, 0, 0):
        # 这不是错误，而是必须显式记录的事实：我们没有用它。
        warnings.append(
            f"apps/desktop/package.json 的版本号是 {desktop_version!r}（占位值），已按设计忽略，"
            f"未作为 Agent 版本使用"
        )

    # ---- 配置可读性
    config_readable = False
    config_error: str | None = None
    config_sha: str | None = None
    config_bytes: int | None = None
    if not config_path.exists():
        config_error = f"配置文件不存在：{config_path}"
    else:
        try:
            from hfk.config.yaml_io import plain_yaml, read_text

            text = read_text(config_path)
            document = plain_yaml().load(text) if text.strip() else {}
            if document is None:
                document = {}
            if not isinstance(document, dict):
                raise ConfigReadError(
                    f"配置顶层不是映射，而是 {type(document).__name__}"
                )
            config_readable = True
            config_sha = sha256_file(config_path)
            config_bytes = config_path.stat().st_size
        except ConfigReadError as exc:
            config_error = exc.message
        except Exception as exc:  # YAML 语法错误等
            config_error = f"配置无法解析：{exc}"

    # ---- 模型供应商（只读 key，不读任何秘密）
    model_provider: str | None = None
    model_name: str | None = None
    if config_readable:
        try:
            from hfk.config.keypath import get_nested
            from hfk.config.yaml_io import plain_yaml, read_text

            document = plain_yaml().load(read_text(config_path)) or {}
            found, value = get_nested(document, "model.provider")
            model_provider = str(value) if found and value else None
            found, value = get_nested(document, "model.default")
            model_name = str(value) if found and value else None
        except Exception:
            pass

    # ---- Node 版本（可选，缺失不影响结论）
    node_version: str | None = None
    node_bin = shutil.which("node")
    if node_bin:
        node_version = _run([node_bin, "--version"], timeout=5.0)
        if node_version:
            node_version = node_version.lstrip("v")

    # ---- 未完成事务
    pending = 0
    try:
        from hfk.engine.transaction import count_pending

        pending = count_pending(hfk_dir)
    except Exception:
        pending = 0

    return HermesEnvironment(
        home=resolved_home,
        config_path=config_path,
        hfk_dir=hfk_dir,
        profile=_profile_for_home(resolved_home),
        install_dir=resolved_install,
        version=str(version) if version else None,
        version_tuple=parse_version(str(version)) if version else None,
        version_source=version_source,
        commit=str(commit) if commit else (str(git["head"]) if git.get("head") else None),
        git_tag=str(git["tag"]) if git.get("tag") else None,
        git_dirty=git_dirty,
        gateway=gateway,
        python_version=platform.python_version(),
        node_version=node_version,
        model_provider=model_provider,
        model_name=model_name,
        config_readable=config_readable,
        config_error=config_error,
        config_sha256=config_sha,
        config_bytes=config_bytes,
        desktop_package_version=desktop_version,
        pending_transactions=pending,
        warnings=tuple(warnings),
    )
