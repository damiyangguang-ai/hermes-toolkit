"""命令行入口：``python -m secengine <子命令>``。

安全边界由命令本身承载：
  * ``recon`` 只做被动检查，不需要授权；
  * ``check`` 需要 ``--authorization`` 指向的有效记录，否则直接拒绝；
  * ``demo`` 只在**自动创建的本机靶场**上跑完整闭环，不接受外部目标参数。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import secrets
import sys
from pathlib import Path

from .checks import (
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
    EvidenceStore,
)
from .recon import run_recon
from .report import build_report


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(delta_hours: float) -> str:
    return (_now() + _dt.timedelta(hours=delta_hours)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- authz


def cmd_authz(args: argparse.Namespace) -> int:
    store = AuthorizationStore(Path(args.file))
    if args.action == "list":
        items = store.load()
        if not items:
            print("没有任何授权记录（因此主动测试一律被拒绝）")
            return 0
        for item in items:
            problems = item.problems()
            status = "有效" if not problems else "无效：" + "；".join(problems)
            print(f"- {item.target}  [{','.join(item.scopes)}]  {status}")
            print(f"    所有者 {item.owner}｜证明 {item.proof}")
        return 0

    if args.action == "check":
        problems = []
        record = store.find_for(args.target, args.scope)
        if record is None:
            for item in store.load():
                if item.target == args.target.strip().lower():
                    problems = item.problems()
            reason = "；".join(problems) if problems else "没有匹配的授权记录"
            print(f"✘ 拒绝：{args.target} 的 {args.scope} —— {reason}")
            return 1
        print(f"✔ 授权有效：{record.owner}（{record.starts_at} → {record.ends_at}）")
        return 0

    # add
    if not args.target or not args.owner or not args.proof:
        print("错误：add 需要 --target / --owner / --proof", file=sys.stderr)
        return 2
    record = Authorization(
        id="authz-" + secrets.token_hex(4),
        target=args.target.strip().lower(),
        owner=args.owner,
        proof=args.proof,
        starts_at=args.starts_at or _iso(-1),
        ends_at=args.ends_at or _iso(24),
        scopes=[item.strip() for item in (args.scopes or "sqli,idor").split(",") if item.strip()],
        forbidden=[item.strip() for item in (args.forbidden or "").split(",") if item.strip()],
        stop_conditions=[item.strip() for item in (args.stop or "").split(",") if item.strip()],
    )
    problems = record.problems()
    if problems:
        print("✘ 授权记录不完整，未保存：" + "；".join(problems), file=sys.stderr)
        return 1
    store.add(record)
    print(f"✔ 已保存授权记录 {record.id}")
    print(f"  目标 {record.target}｜范围 {','.join(record.scopes)}｜窗口 {record.starts_at} → {record.ends_at}")
    return 0


# --------------------------------------------------------------------------- recon


def cmd_recon(args: argparse.Namespace) -> int:
    result = run_recon(args.url)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status else 1
    print(f"目标：{result.url}")
    print(f"状态：{result.status}   错误：{result.error or '无'}")
    print(f"Server：{result.headers.get('Server', '（无）')}")
    if result.tech:
        print("技术栈线索：")
        for item in result.tech:
            version = f" {item['version']}" if item["version"] else ""
            print(f"  - {item['tech']}{version}   ← {item['source']}：{item['match']}")
    if result.tls:
        print(f"TLS：{result.tls.get('protocol')} / {result.tls.get('cipher')}")
        print(f"     证书到期 {result.tls.get('not_after')}")
    if result.findings:
        print(f"响应头问题 {len(result.findings)} 项：")
        for item in result.findings:
            print(f"  - [{item['severity']}] {item['title']}（{item['cwe']}）")
    else:
        print("响应头问题：无")
    return 0


# --------------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    store = AuthorizationStore(Path(args.file))
    evidence_store = EvidenceStore(Path(args.evidence))
    try:
        if args.kind == "sqli":
            _, finding = check_sqli_login(store, args.target, args.base_url, evidence_store=evidence_store)
        elif args.kind == "idor":
            _, finding = check_idor_profile(store, args.target, args.base_url, evidence_store=evidence_store)
        elif args.kind == "xss":
            _, finding = check_xss_search(store, args.target, args.base_url, evidence_store=evidence_store)
        elif args.kind == "ssrf":
            _, finding = check_ssrf_fetch(store, args.target, args.base_url, evidence_store=evidence_store)
        elif args.kind == "privesc":
            _, finding = check_admin_role(store, args.target, args.base_url, evidence_store=evidence_store)
        else:
            print(f"未知类型：{args.kind}", file=sys.stderr)
            return 2
    except AuthorizationError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        print("（这是设计行为：没有授权就不跑主动测试）", file=sys.stderr)
        return 3
    print(f"{finding.id}：{finding.verdict}（{finding.cwe}, {finding.severity}）")
    print(f"证据：{evidence_store.directory / (finding.evidence_id + '.json')}")
    return 0


# --------------------------------------------------------------------------- demo


def cmd_demo(args: argparse.Namespace) -> int:
    """在自建本机靶场上跑一次真实闭环：发现 → 验证 → 修复 → 复测 → 报告。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lab.vulnapp import create_server  # noqa: PLC0415

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    authz_path = out.parent / "authorizations.json"
    store = AuthorizationStore(authz_path)
    demo_scopes = ["sqli", "idor", "xss", "ssrf", "privesc"]

    findings_before = []
    findings_after = []
    recon_before = None
    recon_after = None
    record = None

    # ---- 阶段 1：漏洞版 -------------------------------------------------
    server, port, _ = create_server(0, fixed=False)
    try:
        target = f"127.0.0.1:{port}"
        base = f"http://{target}"
        record = Authorization(
            id="authz-lab-" + secrets.token_hex(3),
            target=target,
            owner="HFK 本地靶场（自有资产）",
            proof="自建靶场，代码在 lab/vulnapp.py，无需第三方授权",
            starts_at=_iso(-1),
            ends_at=_iso(2),
            scopes=list(demo_scopes),
            forbidden=["任何非 127.0.0.1 的目标"],
            stop_conditions=["发现目标不是本机回环地址立即停止"],
        )
        store.add(record)
        print(f"[1/4] 靶场（漏洞版）启动于 {base}")

        before_evidence = EvidenceStore(out.parent / "evidence" / "before")
        recon_before = run_recon(base + "/")
        _, f_headers = check_headers(base + "/", evidence_store=before_evidence)
        _, f_sqli = check_sqli_login(store, target, base, evidence_store=before_evidence)
        _, f_idor = check_idor_profile(store, target, base, evidence_store=before_evidence)
        _, f_xss = check_xss_search(store, target, base, evidence_store=before_evidence)
        _, f_ssrf = check_ssrf_fetch(store, target, base, evidence_store=before_evidence)
        _, f_admin = check_admin_role(store, target, base, evidence_store=before_evidence)
        findings_before = f_headers + [f_sqli, f_idor, f_xss, f_ssrf, f_admin]
        for item in findings_before:
            print(f"      - {item.id}: {item.verdict}")
    finally:
        server.shutdown()
        server.server_close()

    # ---- 阶段 2：修好后的版本 -------------------------------------------
    server2, port2, _ = create_server(0, fixed=True)
    try:
        target2 = f"127.0.0.1:{port2}"
        base2 = f"http://{target2}"
        record2 = Authorization(
            id="authz-lab-fixed-" + secrets.token_hex(3),
            target=target2,
            owner="HFK 本地靶场（自有资产）",
            proof="同上，修复版",
            starts_at=_iso(-1),
            ends_at=_iso(2),
            scopes=list(demo_scopes),
            forbidden=["任何非 127.0.0.1 的目标"],
            stop_conditions=["发现目标不是本机回环地址立即停止"],
        )
        store.add(record2)
        print(f"[2/4] 靶场（修复版）启动于 {base2}")

        after_evidence = EvidenceStore(out.parent / "evidence" / "after")
        recon_after = run_recon(base2 + "/")
        _, a_headers = check_headers(base2 + "/", evidence_store=after_evidence)
        _, a_sqli = check_sqli_login(store, target2, base2, evidence_store=after_evidence)
        _, a_idor = check_idor_profile(store, target2, base2, evidence_store=after_evidence)
        _, a_xss = check_xss_search(store, target2, base2, evidence_store=after_evidence)
        _, a_ssrf = check_ssrf_fetch(store, target2, base2, evidence_store=after_evidence)
        _, a_admin = check_admin_role(store, target2, base2, evidence_store=after_evidence)
        findings_after = a_headers + [a_sqli, a_idor, a_xss, a_ssrf, a_admin]
        for item in findings_after:
            print(f"      - {item.id}: {item.verdict}")
    finally:
        server2.shutdown()
        server2.server_close()

    # ---- 阶段 3：闸门自检 -------------------------------------------------
    print("[3/4] 授权闸门自检（对未授权目标发起主动测试）")
    blocked = False
    reason = ""
    try:
        check_sqli_login(store, "example.com:443", "http://example.com")
    except AuthorizationError as exc:
        blocked = True
        reason = str(exc)
    print(f"      被拒绝：{blocked}")

    # ---- 阶段 4：报告 -----------------------------------------------------
    print("[4/4] 生成报告")
    report = build_report(
        title="HFK Security Research · 本地靶场闭环报告",
        target=record.target if record else "(unknown)",
        authorization=record,
        recon_before=recon_before,
        findings_before=findings_before,
        recon_after=recon_after,
        findings_after=findings_after,
        gate_selfcheck={"blocked": "是" if blocked else "否（异常！）", "reason": reason},
        evidence_dir=str(out.parent / "evidence"),
    )
    out.write_text(report + "\n", encoding="utf-8")
    print(f"      报告：{out}")
    from .report import count_fixed  # noqa: PLC0415

    fixed = count_fixed(findings_before, findings_after)
    print(f"\n完成：修复前存在 {sum(1 for i in findings_before if i.verdict == 'vulnerable')} 项，"
          f"修复后 {sum(1 for i in findings_after if i.verdict == 'vulnerable')} 项，已修复 {fixed} 项")
    return 0


# --------------------------------------------------------------------------- task


def cmd_task(args: argparse.Namespace) -> int:
    """自主任务执行（文档「最高优先级模块」）。"""
    from .report import build_scan_report  # noqa: PLC0415
    from .tasks import TaskEngine  # noqa: PLC0415

    if args.action == "list":
        ledger = Path(args.out) / "tasks"
        items = sorted(ledger.glob("task-*.json")) if ledger.is_dir() else []
        if not items:
            print("还没有任务记录")
            return 0
        for path in items:
            data = json.loads(path.read_text(encoding="utf-8"))
            done = sum(1 for s in data.get("steps") or [] if s.get("status") in ("done", "skipped"))
            print(f"- {data['task_id']}  {data.get('status')}  {done}/{len(data.get('steps') or [])}  {data.get('goal','')[:40]}")
        return 0

    if args.action == "demo":
        return _task_demo(args, build_scan_report, TaskEngine)

    if args.action == "resume":
        if not args.task_id:
            print("错误：resume 需要 --task-id", file=sys.stderr)
            return 2
        engine = TaskEngine(
            AuthorizationStore(Path(args.file)),
            EvidenceStore(Path(args.out) / "evidence"),
            args.out,
        )
        run = engine.resume(args.task_id)
        print(f"任务 {run.task_id} 恢复后状态：{run.status}　进度：{run.progress}")
        return 0 if run.status == "completed" else 1

    # run：对已授权目标自主执行
    if not args.target or not args.base_url:
        print("错误：run 需要 --target 与 --base-url", file=sys.stderr)
        return 2
    engine = TaskEngine(
        AuthorizationStore(Path(args.file)),
        EvidenceStore(Path(args.out) / "evidence"),
        args.out,
    )
    run = engine.plan(args.goal or "对该授权目标执行完整安全检查", args.target, args.base_url)
    print(f"计划：{len(run.steps)} 步 → {', '.join(item.kind for item in run.steps)}")
    engine.run(run)
    # 报告由引擎的 report 步骤真实生成，这里只做校验
    report_path = Path(args.out) / "task-report.md"
    ok, problems = engine.verify(run, report_path)
    print(f"状态：{run.status}　完成校验：{'通过' if ok else '未通过'}")
    for item in problems:
        print(f"  - {item}")
    print(f"报告：{report_path}")
    print(f"账本：{engine.ledger_dir / (run.task_id + '.json')}")
    return 0 if ok else 1


def _task_demo(args: argparse.Namespace, build_scan_report, TaskEngine) -> int:
    """本机靶场上的自主任务闭环：拆解 → 执行 → 校验 → 交付。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lab.vulnapp import create_server  # noqa: PLC0415

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    authz = AuthorizationStore(out / "authorizations.json")
    evidence = EvidenceStore(out / "evidence")

    server, port, _ = create_server(0, fixed=False)
    try:
        target = f"127.0.0.1:{port}"
        base = f"http://{target}"
        record = Authorization(
            id="authz-task-" + secrets.token_hex(3),
            target=target,
            owner="HFK 本地靶场（自有资产）",
            proof="自建靶场，代码在 lab/vulnapp.py",
            starts_at=_iso(-1),
            ends_at=_iso(2),
            scopes=["sqli", "idor", "xss", "ssrf", "privesc"],
            forbidden=["任何非 127.0.0.1 的目标"],
            stop_conditions=["发现目标不是本机回环地址立即停止"],
        )
        authz.add(record)

        engine = TaskEngine(authz, evidence, out)
        run = engine.plan("对该目标执行完整授权安全检查，并交付可复核的报告", target, base)
        print(f"[1/4] 自动拆解：{len(run.steps)} 步 → {', '.join(item.kind for item in run.steps)}")

        print("[2/4] 自主执行（不逐条询问）")
        engine.run(run)
        for step in run.steps:
            print(f"      #{step.index} {step.kind:<9} {step.status:<8} {step.verdict or '—'}")

        print("[3/4] 生成报告并做完成校验（重新读磁盘，不看自述）")
        report_path = out / "task-report.md"
        ok, problems = engine.verify(run, report_path)

        print(f"      校验：{'通过' if ok else '未通过'}")
        for item in problems:
            print(f"        - {item}")

        print("[4/4] 交付")
        print(f"      报告：{report_path}")
        print(f"      账本：{engine.ledger_dir / (run.task_id + '.json')}")
        print(f"      证据：{evidence.directory}")
        print(f"\n任务状态：{run.status}　进度：{run.progress}")
        return 0 if ok else 1
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- 解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secengine",
        description="HFK Security Research Engine：授权范围内的被动侦察与靶场验证",
    )
    parser.add_argument("--file", default="authorizations.json", help="授权记录文件")
    sub = parser.add_subparsers(dest="command", required=True)

    authz = sub.add_parser("authz", help="授权记录管理")
    authz_sub = authz.add_subparsers(dest="action", required=True)
    add = authz_sub.add_parser("add", help="新增授权记录")
    add.add_argument("--target", required=True, help="host 或 host:port")
    add.add_argument("--owner", required=True)
    add.add_argument("--proof", required=True, help="授权证明（工单号/邮件摘要/自建靶场说明）")
    add.add_argument("--starts-at", default=None)
    add.add_argument("--ends-at", default=None)
    add.add_argument("--scopes", default="sqli,idor", help=f"逗号分隔，可选 {','.join(ACTIVE_SCOPES)}")
    add.add_argument("--forbidden", default="")
    add.add_argument("--stop", default="")
    add.set_defaults(func=cmd_authz)
    authz_sub.add_parser("list", help="列出记录").set_defaults(func=cmd_authz)
    check = authz_sub.add_parser("check", help="检查某目标某类型是否已授权")
    check.add_argument("--target", required=True)
    check.add_argument("--scope", required=True, choices=list(ACTIVE_SCOPES))
    check.set_defaults(func=cmd_authz)

    recon = sub.add_parser("recon", help="被动侦察（不需要授权）")
    recon.add_argument("url")
    recon.add_argument("--json", action="store_true")
    recon.set_defaults(func=cmd_recon)

    check_cmd = sub.add_parser("check", help="主动测试（需要有效授权）")
    check_cmd.add_argument("--target", required=True, help="必须与授权记录一致")
    check_cmd.add_argument("--base-url", required=True)
    check_cmd.add_argument("--kind", required=True, choices=["sqli", "idor", "xss", "ssrf", "privesc"])
    check_cmd.add_argument("--evidence", default="evidence")
    check_cmd.set_defaults(func=cmd_check)

    demo = sub.add_parser("demo", help="本机靶场闭环：发现→验证→修复→复测→报告")
    demo.add_argument("--out", default="out/report.md")
    demo.set_defaults(func=cmd_demo)

    task = sub.add_parser("task", help="自主任务执行（拆解→执行→校验→交付）")
    task_sub = task.add_subparsers(dest="action", required=True)
    t_demo = task_sub.add_parser("demo", help="本机靶场上的自主任务闭环")
    t_demo.add_argument("--out", default="out")
    t_demo.set_defaults(func=cmd_task)
    t_run = task_sub.add_parser("run", help="对已授权目标自主执行")
    t_run.add_argument("--target", required=True)
    t_run.add_argument("--base-url", required=True)
    t_run.add_argument("--goal", default="")
    t_run.add_argument("--out", default="out")
    t_run.set_defaults(func=cmd_task)
    t_resume = task_sub.add_parser("resume", help="从 checkpoint 恢复任务")
    t_resume.add_argument("--task-id", required=True)
    t_resume.add_argument("--out", default="out")
    t_resume.set_defaults(func=cmd_task)
    t_list = task_sub.add_parser("list", help="列出任务记录")
    t_list.add_argument("--out", default="out")
    t_list.set_defaults(func=cmd_task)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AuthorizationError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
