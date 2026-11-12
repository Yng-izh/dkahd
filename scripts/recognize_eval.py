#!/usr/bin/env python3
"""识别评估 / 真值 / 扰动 —— 跨图纸准确率与泛化的唯一入口。

子命令：

``run``       跑一张图纸的识别并按真值打分（P/R/F1 + 候选召回 + 几何拓扑）
``bootstrap`` 从一次识别结果导出**草稿真值**（供人工校核，不能当基线）
``compare``   合并多份报告 -> 宏平均（按图纸分组，不跨图纸混样本）
``perturb``   生成受控扰动图纸（图层改名 / 块炸开 / 旋转 / 单位 / 短线切碎）
``invariance`` 泛化验收：**不变性**检验——同一张图经过不该改变语义的扰动后，
              构件集合必须不变。这比准确率更能说明泛化：准确率可以靠对单张图
              调参刷上去，不变性不行。

示例：

    python3 scripts/recognize_eval.py run --drawing 办公楼.dxf --gt gt/office.json \\
        --out work/reports/office.json --md work/reports/office.md

    python3 scripts/recognize_eval.py bootstrap --drawing 办公楼.dxf \\
        --out gt/office.draft.json

    python3 scripts/recognize_eval.py compare --reports work/reports/*.json \\
        --md work/reports/macro.md

    python3 scripts/recognize_eval.py perturb --src 办公楼.dxf \\
        --dst work/perturbed/office_rename.dxf --ops rename_layers
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwg_reader.recognition import evaluation as EV          # noqa: E402
from dwg_reader.recognition import local_solver as LS        # noqa: E402
from dwg_reader.recognition import perturb as PZ             # noqa: E402
from dwg_reader.recognition.pipeline import run_recognition  # noqa: E402
from dwg_reader.pipeline import resolve_dxf                  # noqa: E402


def _run(drawing: str, jobs: int, profile: str | None):
    path = resolve_dxf(drawing)
    run = run_recognition(str(path), jobs=jobs, targets=("assemble",),
                          collect_stats=True, profile=profile,
                          llm_api_key=_llm_key())
    return path, run


def _wall_pool(walls_seg, candidates):
    pool = []
    for w in walls_seg or ():
        if w.get("start") and w.get("end"):
            pool.append({"category": "wall",
                         "geometry": {"start": w["start"], "end": w["end"],
                                      "thickness": w.get("thickness")}})
    for c in candidates or ():
        if c.get("start") and c.get("end"):
            pool.append({"category": "wall",
                         "geometry": {"start": c["start"], "end": c["end"],
                                      "thickness": c.get("thickness")}})
    return pool


def _opening_pool(items, category):
    out = []
    for it in items or ():
        out.append({"category": category,
                    "geometry": {"center": it.get("insert") or it.get("center"),
                                 "width": it.get("width")}})
    return out


def _status(components, state):
    def ratio(cat):
        rows = [c for c in components or () if c.get("category") == cat]
        if not rows:
            return None
        ok = sum(1 for c in rows if c.get("status") == "confirmed")
        return round(ok / len(rows), 4)
    openings = [c for c in components or () if c.get("category") in ("door", "window")]
    open_ratio = (round(sum(1 for c in openings if c.get("status") == "confirmed")
                        / len(openings), 4) if openings else None)
    return {"wall_confirmed_ratio": ratio("wall"),
            "column_confirmed_ratio": ratio("column"),
            "room_confirmed_ratio": ratio("room"),
            "opening_confirmed_ratio": open_ratio,
            "undecided": len((state or {}).get("undecided") or ()),
            "calibration": (state or {}).get("calibration")}


def cmd_run(args) -> int:
    path, run = _run(args.drawing, args.jobs, args.profile)
    out = run.outputs
    comps = out["assemble"]["components"]
    ws = out.get("walls_seg") or {}
    state = out.get("final_candidate_state") or {}
    topo = LS.metrics(state)
    candidates = (_wall_pool(ws.get("walls_seg"), ws.get("candidates"))
                  + _opening_pool(out.get("doors"), "door")
                  + _opening_pool(out.get("windows"), "window")
                  + _opening_pool(out.get("columns"), "column"))
    status = _status(comps, state)
    if args.gt:
        gt = EV.load_gt(args.gt)
        report = EV.score(comps, gt, candidates=candidates, status=status,
                          topology=topo, allow_draft=args.allow_draft,
                          drawing=path.name)
    else:
        report = {"schema": EV.REPORT_SCHEMA, "drawing": path.name,
                  "gt_status": "missing", "final": {}, "candidate": {},
                  "geometry": {}, "status": status, "topology": topo,
                  "run": {"nodes": len(run.stats), "summary": run.summary(),
                          "counts": (out["assemble"].get("meta") or {}).get("counts"),
                          "wall_validation": (out["assemble"].get("meta") or {})
                          .get("wall_validation"),
                          "scope": (out["assemble"].get("meta") or {}).get("scope")}}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"report -> {args.out}")
    md = EV.markdown(report, title=f"识别评估 · {path.name}")
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"markdown -> {args.md}")
    else:
        print(md)
    return 0


def cmd_bootstrap(args) -> int:
    path, run = _run(args.drawing, args.jobs, args.profile)
    comps = run.outputs["assemble"]["components"]
    regions = (run.outputs["load"] or {}).get("regions") or []
    gt = EV.bootstrap(comps, drawing=path.name, regions=regions)
    EV.save_gt(gt, args.out)
    print(f"draft ground truth -> {args.out}"
          f"（{len(gt['walls'])} 墙 / {len(gt['columns'])} 柱 / "
          f"{len(gt['doors'])} 门 / {len(gt['windows'])} 窗 / {len(gt['rooms'])} 房间；"
          f"draft=true，需人工校核后才能作为基线）")
    return 0


def cmd_compare(args) -> int:
    paths = []
    for pattern in args.reports:
        paths.extend(sorted(glob.glob(pattern)))
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    macro = EV.macro(reports)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(macro, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    md = EV.markdown(reports, title="跨图纸宏平均")
    lines = [md, "", "## 宏平均", "",
             "| 类别 | macro P | macro R | macro F1 |", "|---|---|---|---|"]
    for cat, val in macro["categories"].items():
        fmt = lambda v: "—" if v is None else f"{v:.3f}"
        lines.append(f"| {cat} | {fmt(val['macro_precision'])} | "
                     f"{fmt(val['macro_recall'])} | {fmt(val['macro_f1'])} |")
    text = "\n".join(lines) + "\n"
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"markdown -> {args.md}")
    else:
        print(text)
    return 0


#: 默认扰动组合：每一种都**不应改变**图纸语义，只改变表达方式
INVARIANCE_OPS = ("rename_layers", "explode", "rotate", "split_lines", "scale")
#: scale 扰动用这个倍数（mm -> m）；识别器必须靠几何证据把单位纠回来
INVARIANCE_SCALE = 0.001
#: 数量不变性容差：扰动前后构件数允许的相对变化
COUNT_TOL = 0.05


def _perturb_self_check(op: str, applied, args) -> str | None:
    """返回 None 表示扰动自检通过，否则返回问题描述。"""
    a = (applied or [{}])[0] or {}
    if a.get("failed"):
        return f"有 {a['failed']} 个图元变换失败（{a.get('failed_sample')}）"
    if op == "scale":
        want = float(args.factor)
        got = a.get("span_ratio")
        if got is None:
            return "扰动后拿不到内容包围盒，无法自检倍数"
        if abs(got / want - 1.0) > 0.02:
            return f"倍数被施加了 {got / want:.3g} 次（要 {want}，实测 {got}）"
    if op == "rotate":
        got = a.get("span_ratio")
        if got is not None and abs(got - 1.0) > 0.05:
            return f"旋转后包围盒跨度变了 {got:.3g} 倍（应为 1）"
    return None


def _counts(run) -> dict:
    comps = run.outputs["assemble"]["components"]
    out = {}
    for c in comps:
        out[c["category"]] = out.get(c["category"], 0) + 1
    return out


#: 是否让模型参与（不变性检验的默认是"关"）。
#: 原因：不变性要测的是**确定性几何核心**稳不稳；模型参与进来以后同一张图两次
#: 跑都可能不同，任何"偏差"都无法归因。而且块炸开的图上墙网退化，洞口候选会
#: 从几十个涨到上百个，逐个问模型会把一次检验拖成几十分钟。
_USE_LLM = False


def _llm_key() -> str | None:
    return (os.environ.get("LLM_API_KEY") or None) if _USE_LLM else None


def cmd_invariance(args) -> int:
    """同一张图 + 不该改变语义的扰动 -> 构件数量必须不变。

    这是"泛化"的可操作定义：不是"这张图准"，而是"换一种画法还认得出"。
    """
    global _USE_LLM
    _USE_LLM = bool(getattr(args, "llm", False))
    src = Path(resolve_dxf(args.drawing))
    workdir = Path(args.workdir or (src.parent / "work" / "invariance"))
    workdir.mkdir(parents=True, exist_ok=True)
    ops_list = [o.strip() for o in (args.ops or ",".join(INVARIANCE_OPS)).split(",") if o.strip()]

    base_run = run_recognition(str(src), jobs=args.jobs, targets=("assemble",),
                               collect_stats=True, llm_api_key=_llm_key(),
                               profile=args.profile)
    base = _counts(base_run)
    rows = []
    for op in ops_list:
        dst = workdir / f"{src.stem}.{op}.dxf"
        try:
            info = PZ.apply(str(src), str(dst), [op], prefix=args.prefix,
                            degrees=args.degrees, factor=args.factor,
                            segments=args.segments)
        except Exception as exc:                       # 扰动本身不成立就跳过并留痕
            rows.append({"op": op, "status": "skipped",
                         "why": f"{type(exc).__name__}: {exc}"})
            continue
        # ---- 工具自检：扰动必须先证明自己是对的，再谈识别器稳不稳。
        # 没有这一步，工具 bug 会被读成"识别退化"（曾经就发生过：模型空间被遍历
        # 两遍，rotate 实际转了 180°、scale 变成百万分之一）。
        bad = _perturb_self_check(op, info.get("applied") or [{}], args)
        if bad:
            rows.append({"op": op, "status": "tool_error", "why": bad,
                         "perturb": info})
            continue
        try:
            run = run_recognition(str(dst), jobs=args.jobs, targets=("assemble",),
                                  collect_stats=True, llm_api_key=_llm_key(),
                                  profile=args.profile)
            got = _counts(run)
        except Exception as exc:
            rows.append({"op": op, "status": "failed",
                         "why": f"{type(exc).__name__}: {exc}",
                         "perturb": info})
            continue
        cats = sorted(set(base) | set(got))
        delta = {}
        worst, worst_cat = 0.0, None
        for c in cats:
            b, g = base.get(c, 0), got.get(c, 0)
            rel = abs(g - b) / b if b else (0.0 if g == 0 else 1.0)
            delta[c] = {"base": b, "got": g, "rel": round(rel, 4)}
            if b and rel > worst:
                worst, worst_cat = rel, c
        rows.append({"op": op,
                     "status": "ok" if worst <= COUNT_TOL else "drift",
                     "worst": worst_cat, "worst_rel": round(worst, 4),
                     "total": {"base": sum(base.values()), "got": sum(got.values())},
                     "delta": delta, "perturb": info})

    ok = [r for r in rows if r.get("status") == "ok"]
    tool_bad = [r for r in rows if r.get("status") == "tool_error"]
    report = {"schema": "invariance-report/1.0", "drawing": src.name,
              "tolerance": COUNT_TOL, "base": base,
              "base_total": sum(base.values()),
              "rows": rows,
              "summary": {"ops": len(rows), "ok": len(ok),
                          "drift": sum(1 for r in rows if r.get("status") == "drift"),
                          "tool_error": len(tool_bad),
                          "skipped": sum(1 for r in rows if r.get("status") == "skipped"),
                          "failed": sum(1 for r in rows if r.get("status") == "failed")}}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"invariance -> {args.out}")
    lines = [f"# 不变性检验 · {src.name}", "",
             f"基线构件数 **{report['base_total']}**，容差 ±{int(COUNT_TOL * 100)}%", "",
             "| 扰动 | 状态 | 基线 | 扰动后 | 最差类别 | 偏差 |", "|---|---|---|---|---|---|"]
    for r in rows:
        t = r.get("total") or {}
        lines.append(f"| {r['op']} | {r['status']} | {t.get('base', '—')} | "
                     f"{t.get('got', '—')} | {r.get('worst') or '—'} | "
                     f"{('%.1f%%' % (100 * r['worst_rel'])) if r.get('worst_rel') is not None else '—'} |")
    text = "\n".join(lines) + "\n"
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"markdown -> {args.md}")
    else:
        print(text)
    return 0 if report["summary"]["drift"] == 0 else 1


def cmd_perturb(args) -> int:
    ops = [op.strip() for op in (args.ops or "").split(",") if op.strip()]
    info = PZ.apply(args.src, args.dst, ops, prefix=args.prefix,
                    degrees=args.degrees, factor=args.factor,
                    segments=args.segments)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="识别评估 / 真值 / 扰动")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑识别并按真值打分")
    r.add_argument("--drawing", required=True)
    r.add_argument("--gt")
    r.add_argument("--out")
    r.add_argument("--md")
    r.add_argument("--jobs", type=int, default=int(os.environ.get("DWG_JOBS", "4")))
    r.add_argument("--profile")
    r.add_argument("--allow-draft", action="store_true",
                   help="允许用草稿真值算精度（结果不能当基线）")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bootstrap", help="导出草稿真值")
    b.add_argument("--drawing", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--jobs", type=int, default=int(os.environ.get("DWG_JOBS", "4")))
    b.add_argument("--profile")
    b.set_defaults(func=cmd_bootstrap)

    c = sub.add_parser("compare", help="多份报告 -> 宏平均")
    c.add_argument("--reports", nargs="+", required=True)
    c.add_argument("--out")
    c.add_argument("--md")
    c.set_defaults(func=cmd_compare)

    p = sub.add_parser("perturb", help="生成受控扰动图纸")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--ops", required=True,
                   help="rename_layers,explode,rotate,scale,split_lines")
    p.add_argument("--prefix", default="PZ_")
    p.add_argument("--degrees", type=float, default=90.0)
    p.add_argument("--factor", type=float, default=0.001)
    p.add_argument("--segments", type=int, default=3)
    p.set_defaults(func=cmd_perturb)

    v = sub.add_parser("invariance", help="泛化验收：语义不变性检验")
    v.add_argument("--drawing", required=True)
    v.add_argument("--ops", help="逗号分隔的扰动，默认 " + ",".join(INVARIANCE_OPS))
    v.add_argument("--workdir")
    v.add_argument("--out")
    v.add_argument("--md")
    v.add_argument("--jobs", type=int, default=int(os.environ.get("DWG_JOBS", "4")))
    v.add_argument("--profile")
    v.add_argument("--prefix", default="PZ_")
    v.add_argument("--degrees", type=float, default=90.0)
    v.add_argument("--factor", type=float, default=0.001)
    v.add_argument("--segments", type=int, default=3)
    v.add_argument("--llm", action="store_true",
                   help="让模型参与（默认关：不变性检验只测确定性几何核心）")
    v.set_defaults(func=cmd_invariance)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
