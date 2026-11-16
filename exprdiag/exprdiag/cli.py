"""命令行入口：``python -m exprdiag <子命令>``。

只读诊断 + 有界能力矩阵。不修改任何文件、不读取任何密钥文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compat import (
    EXCLUDED_PROBES,
    PROBE_CLASSES,
    MockProvider,
    OpenAICompatProvider,
    render_matrix,
    run_matrix,
)
from .inspect import classify_refusal, diagnose, render_markdown


def _default_home() -> Path | None:
    import os

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "hermes"
        if candidate.exists():
            return candidate
    return None


def _default_install(home: Path | None) -> Path | None:
    if home is None:
        return None
    candidate = home / "hermes-agent"
    return candidate if candidate.exists() else None


def cmd_diag(args: argparse.Namespace) -> int:
    home = Path(args.home) if args.home else _default_home()
    if home is None:
        print("错误：找不到 Hermes Home，请用 --home 指定", file=sys.stderr)
        return 2
    install = Path(args.install_dir) if args.install_dir else _default_install(home)
    report = diagnose(home, install)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.json:
            out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            out.write_text(render_markdown(report) + "\n", encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


def cmd_compat(args: argparse.Namespace) -> int:
    providers = []
    if args.mock:
        providers.append(MockProvider())
    for spec in args.provider or []:
        # 格式：name|base_url|model|API_KEY_ENV
        parts = [item.strip() for item in spec.split("|")]
        if len(parts) != 4:
            print(f"错误：--provider 需要 4 段（name|base_url|model|API_KEY_ENV），收到 {spec}", file=sys.stderr)
            return 2
        name, base_url, model, env_name = parts
        providers.append(
            OpenAICompatProvider(name=name, base_url=base_url, model=model, api_key_env=env_name)
        )
    if not providers:
        print(
            "错误：至少给一个提供方。\n"
            "  验证矩阵逻辑： --mock\n"
            "  真实模型：     --provider \"名称|https://api.example.com/v1|模型名|API_KEY_ENV\""
            ,
            file=sys.stderr,
        )
        return 2

    matrix = run_matrix(providers)
    text = render_matrix(matrix)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        out.with_suffix(".json").write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写入 {out} 与 {out.with_suffix('.json')}")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    result = classify_refusal(args.text, request_sent=(True if args.sent else None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_probes(args: argparse.Namespace) -> int:
    print("探测任务类：")
    for probe in PROBE_CLASSES:
        print(f"- {probe.key}：{probe.title}（期望 {probe.expectation}）")
    print("\n显式排除项：")
    for item in EXCLUDED_PROBES:
        print(f"- {item['item']}：{item['reason']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exprdiag",
        description="Expression Engine 诊断：提示层检查 / 提示词冲突 / 拒绝归因 / 能力矩阵（只读）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diag", help="逐层检查提示词与内容限制（只读）")
    diag.add_argument("--home", default=None)
    diag.add_argument("--install-dir", default=None)
    diag.add_argument("--json", action="store_true")
    diag.add_argument("--out", default=None)
    diag.set_defaults(func=cmd_diag)

    compat = sub.add_parser("compat", help="模型能力矩阵（有界，不含露骨内容探测）")
    compat.add_argument("--mock", action="store_true", help="用确定性假提供方验证矩阵逻辑")
    compat.add_argument(
        "--provider",
        action="append",
        help='真实提供方，格式 "name|base_url|model|API_KEY_ENV"，可重复',
    )
    compat.add_argument("--out", default=None)
    compat.set_defaults(func=cmd_compat)

    classify = sub.add_parser("classify", help="判断一次拒绝发生在哪一层")
    classify.add_argument("--text", required=True, help="错误信息或响应片段")
    classify.add_argument("--sent", action="store_true", help="已确认请求发送给了模型")
    classify.set_defaults(func=cmd_classify)

    probes = sub.add_parser("probes", help="列出探测任务类与排除项")
    probes.set_defaults(func=cmd_probes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
