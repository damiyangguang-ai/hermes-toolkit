"""HFK 命令行入口。

Phase 2 要求「核心引擎必须能独立运行，不依赖 Desktop UI」——本文件就是那条独立通路。

用法示例：
    hfk env                      # 环境报告
    hfk presets                  # 四个预设分别会改什么
    hfk preview --preset balanced
    hfk apply --preset balanced
    hfk status
    hfk restore                  # 逆序恢复全部 pending 事务
    hfk restore --id tx-...
    hfk uninstall
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hfk.errors import HFKError
from hfk.facade import HFK
from hfk.schema import get_spec
from hfk.version import HFK_LONG_NAME, HFK_VERSION

__all__ = ["main"]


# --------------------------------------------------------------------------- 参数解析


def parse_assignment(text: str) -> tuple[str, Any]:
    """把 ``path=value`` 解析成 (path, value)。

    关键：**不**用 YAML 解析标量。因为在 YAML 1.1 下 ``off`` 会被读成布尔 ``False``，
    而 ``approvals.mode`` 的合法值恰恰就是字符串 ``off``。所以按字段类型分别解析。
    """
    if "=" not in text:
        raise SystemExit(f"--set 需要 path=value 形式，收到：{text!r}")
    path, _, raw = text.partition("=")
    path = path.strip()
    raw = raw.strip()
    spec = get_spec(path)
    if spec is None:
        # 交给下游 schema 校验给出更准确的报错
        return path, raw

    if spec.kind == "bool":
        lowered = raw.lower()
        if lowered in ("true", "1", "yes", "on"):
            return path, True
        if lowered in ("false", "0", "no", "off"):
            return path, False
        raise SystemExit(f"{path} 需要布尔值（true/false），收到 {raw!r}")
    if spec.kind == "int":
        try:
            return path, int(raw)
        except ValueError as exc:
            raise SystemExit(f"{path} 需要整数，收到 {raw!r}") from exc
    if spec.kind == "list":
        if raw == "":
            return path, []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in raw.split(",") if item.strip()]
        return path, parsed
    # enum / str：保持原样字符串（不要做 YAML 标量推断）
    return path, raw


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hfk",
        description=f"{HFK_LONG_NAME} {HFK_VERSION} —— Hermes 本地审批策略补丁工具",
    )
    parser.add_argument("--home", help="显式指定 Hermes Home（默认自动探测）")
    parser.add_argument("--install-dir", help="显式指定 Hermes 安装目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于脚本/UI 消费）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="输出宿主环境报告")
    sub.add_parser("status", help="补丁状态、未完成事务、备份数量")
    sub.add_parser("catalogue", help="列出已验证字段清单与不可修改项")
    sub.add_parser("presets", help="列出四个预设各自会改什么")
    sub.add_parser("transactions", help="列出全部事务记录")
    sub.add_parser("backups", help="列出备份文件")

    for name, help_text in (
        ("preview", "预览补丁差异（不写入）"),
        ("apply", "执行补丁（自动备份 + 事务记录）"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--preset", default=None, help="preset：conservative/balanced/freedom/custom")
        node.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                          help="逐项覆盖，可重复，例如 --set approvals.timeout=120")
        if name == "apply":
            node.add_argument("--acknowledge-risk", action="store_true",
                              help="确认接受 freedom 预设的风险")

    restore_node = sub.add_parser("restore", help="恢复原始配置（字段级、检测冲突）")
    restore_node.add_argument("--id", default=None, help="只恢复指定事务 ID")

    uninstall_node = sub.add_parser("uninstall", help="卸载 HFK（先安全恢复，再清理自身文件）")
    uninstall_node.add_argument("--keep-plugin", action="store_true", help="保留 Desktop 插件目录")

    sub.add_parser("selftest", help="运行内置自检（YAML 语义、事务、冲突恢复）")

    service_node = sub.add_parser("service", help="启动本地 HFK Core 服务（仅 127.0.0.1）")
    service_node.add_argument("--port", type=int, default=0, help="端口（0 = 随机）")
    service_node.add_argument("--print-token", action="store_true", help="把令牌打印到 stdout 供前端注入")

    return parser


# --------------------------------------------------------------------------- 输出


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_env(report: dict[str, Any]) -> None:
    gateway = report.get("gateway") or {}
    compat = report.get("compat") or {}
    print(f"{HFK_LONG_NAME} {HFK_VERSION}")
    print("=" * 62)
    print(f"Hermes 版本      : {report.get('version') or '未知'}  （来源 {report.get('version_source')}）")
    print(f"兼容性          : {'✔ 支持' if compat.get('supported') else '✘ 不支持'} — {compat.get('reason')}")
    print(f"Hermes Home     : {report.get('home')}")
    print(f"安装目录        : {report.get('install_dir') or '未找到'}")
    print(f"当前 Profile    : {report.get('profile')}")
    print(f"配置文件        : {report.get('config_path')}")
    print(f"配置可读        : {'是' if report.get('config_readable') else '否'} "
          f"({report.get('config_bytes') or 0} 字节)")
    if report.get("config_error"):
        print(f"配置问题        : {report['config_error']}")
    print(f"Gateway         : {'运行中 pid ' + str(gateway.get('pid')) if gateway.get('running') else '未运行'}"
          f" [{gateway.get('state')}]")
    print(f"Python / Node   : {report.get('python_version')} / {report.get('node_version') or '未找到'}")
    print(f"模型供应商      : {report.get('model_provider') or '未知'}"
          f" （{report.get('model_name') or '模型未设置'}）")
    print(f"未完成事务      : {report.get('pending_transactions')}")
    print(f"HFK 数据目录    : {report.get('hfk_dir')}")
    if report.get("desktop_package_version"):
        print(f"Desktop 包版本  : {report['desktop_package_version']}（占位值，未用于版本判定）")
    warnings = report.get("warnings") or []
    if warnings:
        print("-" * 62)
        print("提示：")
        for item in warnings:
            print(f"  · {item}")


def _print_plan(plan: dict[str, Any]) -> None:
    compat = plan.get("compat") or {}
    print(f"补丁预览（预设 {plan.get('preset')}）")
    print(f"配置文件: {plan.get('config_path')}")
    print(f"当前哈希: {plan.get('expected_sha256')}")
    print("-" * 62)
    for change in plan.get("changes") or []:
        mark = "改" if change.get("changed") else "略"
        risk = change.get("risk", "?")
        print(f"[{mark}][{risk}] {change.get('path')}")
        print(f"        {change.get('title')}")
        print(f"        {change.get('display')}")
        if change.get("error"):
            print(f"        ✘ {change['error']}")
    print("-" * 62)
    print(f"将写入 {sum(1 for c in plan.get('changes') or [] if c.get('changed'))} 项；"
          f"兼容性：{'支持' if compat.get('supported') else '不支持'}")
    for warning in plan.get("warnings") or []:
        print(f"⚠ {warning}")
    for error in plan.get("errors") or []:
        print(f"✘ {error}")


# --------------------------------------------------------------------------- 主逻辑


def _run(args: argparse.Namespace) -> int:
    if args.command == "selftest":
        from hfk.selftest import run_selftest

        return run_selftest()

    hfk = HFK.connect(home=args.home, install_dir=args.install_dir)

    if args.command == "env":
        report = hfk.env_report()
        if args.json:
            _emit(report, True)
        else:
            _print_env(report)
        return 0

    if args.command == "status":
        payload = hfk.status()
        if args.json:
            _emit(payload, True)
        else:
            print(f"HFK {payload['hfk_version']} — 状态")
            print("-" * 62)
            print(f"Hermes 版本     : {payload['hermes_version']}（{'支持' if payload['supported'] else '不支持'}）")
            print(f"Profile         : {payload['profile']}")
            print(f"未完成事务      : {payload['pending_transactions']}")
            print(f"事务总数        : {payload['total_transactions']}")
            print(f"备份数量        : {payload['backups']}")
            print(f"最近 Apply      : {payload['last_apply'] or '无'}")
            print(f"最近 Restore    : {payload['last_restore'] or '无'}")
            if payload["active_fields"]:
                print("生效中的字段：")
                for path, value in payload["active_fields"].items():
                    print(f"  · {path} = {value}")
            else:
                print("生效中的字段    : 无（当前没有 HFK 补丁生效）")
        return 0

    if args.command == "catalogue":
        payload = hfk.catalogue()
        if args.json:
            _emit(payload, True)
            return 0
        print("已验证字段清单（每项都有源码证据）")
        print("=" * 62)
        for field in payload["fields"]:
            print(f"[{field['risk']}] {field['path']}  —  {field['title']}")
            print(f"      类型: {field['type']}；默认: {field['default']!r}")
            print(f"      作用: {field['effect']}")
            print(f"      证据: {field['evidence']}")
        print("-" * 62)
        print("HFK 明确不修改（C 类）：")
        for item in payload["blocked"]:
            print(f"  · {item['path']}：{item['reason']}")
        print("代码级安全底线（不存在配置开关，因此无法被本工具削弱）：")
        for item in payload["code_level_floors"]:
            print(f"  · {item['name']}：{item['detail']}")
        return 0

    if args.command == "presets":
        payload = hfk.presets()
        if args.json:
            _emit(payload, True)
            return 0
        print(f"默认预设：{payload['default']}")
        for key, preset in payload["items"].items():
            print("=" * 62)
            print(f"{key}  ——  {preset['label']}")
            print(f"  {preset['description']}")
            if preset["risk_note"]:
                print(f"  风险: {preset['risk_note']}")
            if preset["requires_acknowledgement"]:
                print("  ⚠ 应用时需要 --acknowledge-risk")
            for change in preset["changes"]:
                print(f"    · {change['path']} → {change['value']}")
            if not preset["changes"]:
                print("    （不预设任何字段）")
        return 0

    if args.command in ("preview", "apply"):
        overrides = dict(parse_assignment(item) for item in args.set)
        if args.command == "preview":
            payload = hfk.preview(preset=args.preset, overrides=overrides)
            if args.json:
                _emit(payload, True)
            else:
                _print_plan(payload)
            return 0 if not payload.get("errors") else 1

        payload = hfk.apply(
            preset=args.preset,
            overrides=overrides,
            acknowledge_risk=getattr(args, "acknowledge_risk", False),
        )
        if args.json:
            _emit(payload, True)
        else:
            print("✔ 补丁已应用")
            print("-" * 62)
            print(f"事务 ID   : {payload['transaction_id']}")
            print(f"备份文件  : {payload['backup_path']}")
            print(f"原始哈希  : {payload['before_sha256']}")
            print(f"修改后哈希: {payload['after_sha256']}")
            for change in payload.get("changes") or []:
                print(f"  · {change['path']}: {change['before']!r} → {change['after']!r}")
        return 0

    if args.command == "restore":
        payload = hfk.restore(args.id)
        if args.json:
            _emit(payload, True)
        else:
            print(f"恢复结果：{payload['status']}")
            print("-" * 62)
            for path in payload["restored"]:
                print(f"  ✔ 已还原 {path}")
            for conflict in payload["conflicts"]:
                print(f"  ⚠ 冲突 {conflict['path']}：{conflict['reason']}")
                print(f"      当前值 {conflict['current']!r}，HFK 曾写入 {conflict['hfk_wrote']!r}，"
                      f"原始值 {conflict['original']!r}")
            for warning in payload["warnings"]:
                print(f"  · {warning}")
        return 0

    if args.command == "transactions":
        payload = hfk.transactions()
        if args.json:
            _emit(payload, True)
            return 0
        print(f"事务共 {payload['count']} 条")
        for tx in payload["transactions"]:
            print("-" * 62)
            print(f"{tx['transaction_id']}  [{tx['status']}]  {tx['timestamp']}")
            print(f"  预设 {tx.get('preset')}，Hermes {tx['hermes_version']}，Profile {tx['profile']}")
            print(f"  备份 {tx['backup_path']}")
            for change in tx["changes"]:
                print(f"    · {change['path']}: {change['before']!r} → {change['after']!r} ({change['status']})")
        return 0

    if args.command == "backups":
        payload = hfk.backups()
        if args.json:
            _emit(payload, True)
            return 0
        print(f"备份目录：{payload['directory']}")
        print(f"共 {payload['count']} 个备份")
        for item in payload["files"]:
            print(f"  · {item['name']}  ({item['size']} 字节)")
        return 0

    if args.command == "uninstall":
        payload = hfk.uninstall(remove_plugin=not args.keep_plugin)
        if args.json:
            _emit(payload, True)
        else:
            print("卸载完成")
            print("-" * 62)
            print(f"恢复状态  : {payload['restore']['status']}")
            print(f"已删插件  : {'是' if payload['removed_plugin'] else '否'}")
            print(f"保留备份  : {payload['kept_backups']} 个")
            print(f"卸载记录  : {payload['record_path']}")
            for note in payload["notes"]:
                print(f"  · {note}")
        return 0

    if args.command == "service":
        from hfk.service.app import serve

        return serve(port=args.port, print_token=args.print_token)

    raise SystemExit(f"未知命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except HFKError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
            if exc.detail:
                print(f"详情：{exc.detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断（未完成的写入已回滚）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
