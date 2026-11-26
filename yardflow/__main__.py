"""命令行：读取 samples 交换资料，输出逐车核放/申报行档案。

用法::

    python3 -m yardflow                 # 读取 ./samples，中文摘要
    python3 -m yardflow --json          # 完整 JSON 报告
    python3 -m yardflow --at 2026-11-27T03:20:00Z
    python3 -m yardflow --dir samples --line A-700/L1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import ISSUE_TEXT, Service, ts_text


def _line_ref(line) -> str:
    return f"{line['submission']}/{line['line']}"


def render_text(svc: Service, at: str | None, line_only: str | None = None) -> str:
    report = svc.report(at)
    when = ts_text(report["as_of"])
    out: list[str] = []
    out.append(f"整车拼报拆单与分票核放报告  时点 {when}")
    out.append("=" * 68)

    if line_only:
        sub, _, lid = line_only.partition("/")
        dossier = svc.line_dossier(sub, lid, at)
        out.extend(_render_dossier(dossier))
        return "\n".join(out)

    out.append("")
    out.append("【逐车状态】")
    for v in report["vins"]:
        inv = v.get("invoice") or {}
        caliber = f"{inv.get('commodity')}/{inv.get('trade')}/{inv.get('unit')}" if inv else "无发票"
        basis = (v.get("basis") or {}).get("permit") if v.get("basis") else None
        if v["physical_state"] == "departed":
            head = f"  {v['vin']}  已离场({(v.get('run') or '')})  使用核放 {v.get('permit_used')}"
        elif v["physical_state"] == "staged":
            head = f"  {v['vin']}  已装 {v.get('run')}  有效依据 {basis or '无'}"
        else:
            head = f"  {v['vin']}  在库  有效依据 {basis or '无'}"
        out.append(head + f"  口径 {caliber}")
        for issue in v.get("issues", []):
            code = issue.get("code")
            out.append(f"      ! 问题: {ISSUE_TEXT.get(code, code)}")
        for todo in v.get("todos", []):
            out.append(f"      → 待办: {todo.get('text') or todo.get('code')}")
        fu = v.get("follow_up")
        if fu:
            out.append(f"      ⚑ 后续处置[{fu['owner']}]: {fu['instruction']}")

    out.append("")
    out.append(f"【当前可核放】 {', '.join(report['releasable_vins']) or '（无）'}")

    unsupported = report["unsupported_staged"]
    out.append("")
    out.append("【已装车但无有效依据 —— 待卸 / 留场】")
    if not unsupported:
        out.append("  无")
    for u in unsupported:
        out.append(
            f"  {u['vin']} @ {u['run']}（装车 {ts_text(u['staged_at'])}，"
            f"出示 {u['permit_presented'] or '无'}）: {u['reason']}"
        )
        out.append("      处理: 卸车(unload) 或 留场(hold_yard)；旧装车清单不得出区")

    out.append("")
    out.append("【申报行单证对账】")
    for d in report["lines"]:
        out.extend(_render_dossier(d, indent="  "))

    if report["customs_affairs"]:
        out.append("")
        out.append("【关务处置名单】")
        for c in report["customs_affairs"]:
            out.append(f"  {c['vin']}: {c['follow_up']['instruction']}")

    out.append("")
    out.append("说明: 可传 --at <ISO时间> 查看不同时点（如 03:20 后 V311 回执过期）。")
    return "\n".join(out)


def _render_dossier(d: dict, indent: str = "") -> list[str]:
    p = indent
    g = d.get("grouping")
    g_text = json.dumps(g, ensure_ascii=False) if g else "-"
    amt = d["amounts"]
    match_text = {True: "一致", False: "不一致", None: "未登记申报额"}[amt["matches"]]
    lines = [
        f"{p}◇ {d['submission']}/{d['line']}  状态={d['status']}"
        + ("  [回告建档]" if d.get("external_record") else ""),
        f"{p}  口径={g_text}  金额(发票合计/申报)="
        f"{amt['invoice_total_cents']}/{amt['declared_cents']}  核对={match_text}",
        f"{p}  组成车辆: {', '.join(d['composition'])}",
    ]
    for v in d["vehicles"]:
        used = v.get("permit_used_on_departure")
        lines.append(
            f"{p}    - {v['vin']} rev{v.get('revision')} 状态={v['physical_state']}"
            f" 班车={v.get('run') or '-'} 出场实用核放={used or '-'}"
            f" 当前有效依据={v.get('valid_basis') or '-'} 可核放={v['releasable']}"
        )
        for issue in v.get("issues", []):
            lines.append(f"{p}        ! {ISSUE_TEXT.get(issue.get('code'), issue.get('code'))}")
        for todo in v.get("todos", []):
            lines.append(f"{p}        → {todo.get('text') or todo.get('code')}")
        fu = v.get("follow_up")
        if fu:
            lines.append(f"{p}        ⚑ [{fu['owner']}] {fu['instruction']}")
    for r in d.get("receipts", []):
        lines.append(
            f"{p}  回告 {r['event']} {r['disposition']} vins={r['vins']} "
            f"permit={r.get('permit') or '-'} at={ts_text(r['at'])}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整车拼报拆单与分票核放服务")
    parser.add_argument("--dir", default="samples", help="JSONL 交换资料目录")
    parser.add_argument("--at", default=None, help="报告时点（ISO 8601，如 2026-11-27T02:05:00Z）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--line", default=None, help="只看某申报行，格式 A-700/L1")
    args = parser.parse_args(argv)

    svc = Service(clock=args.at)
    counts = svc.load_directory(Path(args.dir))
    if args.json:
        if args.line:
            sub, _, lid = args.line.partition("/")
            payload = svc.line_dossier(sub, lid, args.at)
        else:
            payload = svc.report(args.at)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print(render_text(svc, args.at, args.line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
