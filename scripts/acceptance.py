#!/usr/bin/env python3
"""真实图纸验收：真值 vs 识别结果，按类别逐项判定通过/不通过。

真值文件（``work/gt/*.json``）是**独立于识别器**量出来的：用 ezdxf 直接数块参照、
4 线矩形配对、开启弧、房名文字，不经过本技能任何识别代码。格式：

    {
      "schema": "acceptance/1.0",
      "drawing": "办公楼.dxf",
      "truth": {"wall": 31, "column": 38, "door": 30, "window": 31, "room": 21},
      "tolerance": {"window": 1},
      "criterion": {"column": "COLUMN 图层 _FZH 块参照计数", ...}
    }

真值可以是整数（精确目标）或 ``[下限, 上限]``（区间目标，例如"开启弧数 ≤ 门数 ≤
弧数×1.5"）。判定：

* 整数真值：``|识别 - 真值| <= tolerance[类别]``（默认 0）
* 区间真值：``下限 <= 识别 <= 上限``

用法::

    python3 scripts/acceptance.py --truth work/gt/office.json \\
        --truth work/gt/tdw.json --out work/reports/acceptance.json

退出码：全部通过 0，有项目不通过 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwg_reader.recognition.pipeline import run_recognition   # noqa: E402
from dwg_reader.pipeline import resolve_dxf                   # noqa: E402

#: 报告里展示顺序（也是"哪些类别必须达标"的顺序）
CATEGORIES = ("wall", "column", "door", "window", "room")
CN = {"wall": "墙", "column": "柱", "door": "门", "window": "窗", "room": "房间"}


def counts(run) -> dict:
    out = {}
    for c in run.outputs["assemble"]["components"]:
        out[c["category"]] = out.get(c["category"], 0) + 1
    return out


def compare(kind: str, truth, tol: int, got: int) -> dict:
    if isinstance(truth, (list, tuple)) and len(truth) == 2:
        lo, hi = truth
        ok = lo <= got <= hi
        return {"target": f"{lo}..{hi}", "got": got, "ok": ok,
                "delta": None, "note": "区间目标"}
    t = int(truth)
    delta = got - t
    ok = abs(delta) <= int(tol)
    return {"target": t, "got": got, "ok": ok, "delta": delta,
            "note": "精确目标" if abs(delta) <= int(tol) else
                    ("多认" if delta > 0 else "漏认")}


def one(truth_path: str, jobs: int) -> dict:
    spec = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    path = Path(resolve_dxf(spec["drawing"]))
    run = run_recognition(str(path), jobs=jobs, targets=("assemble",),
                          collect_stats=True)
    got = counts(run)
    tol = spec.get("tolerance") or {}
    rows, all_ok = {}, True
    for cat in CATEGORIES:
        if cat not in (spec.get("truth") or {}):
            continue
        row = compare(cat, spec["truth"][cat], tol.get(cat, 0), got.get(cat, 0))
        row["criterion"] = (spec.get("criterion") or {}).get(cat)
        rows[cat] = row
        all_ok = all_ok and row["ok"]
    return {"drawing": path.name, "ok": all_ok, "rows": rows,
            "counts": got, "total": sum(got.values()),
            "duration_s": round(run.duration_ms / 1000.0, 1),
            "failed_nodes": [s.name for s in run.stats if s.status == "failed"],
            "scale": ((run.outputs.get("load") or {}).get("scope") or {}).get("scale")}


def markdown(results) -> str:
    lines = ["# 真实图纸验收", ""]
    lines.append("| 图纸 | 类别 | 真值 | 识别 | 判定 | 真值口径 |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        for cat, row in r["rows"].items():
            verdict = "✅ 通过" if row["ok"] else f"❌ {row['note']}"
            lines.append(f"| {r['drawing']} | {CN.get(cat, cat)} | {row['target']} | "
                         f"{row['got']} | {verdict} | {row.get('criterion') or '—'} |")
    lines.append("")
    for r in results:
        lines.append(f"* {r['drawing']}：构件合计 {r['total']}，用时 {r['duration_s']}s，"
                     f"失败节点 {r['failed_nodes'] or '无'}")
    passed = sum(1 for r in results if r["ok"])
    lines.append(f"* **总体：{passed}/{len(results)} 张图纸全部达标**")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="真实图纸验收")
    ap.add_argument("--truth", nargs="+", required=True, help="真值 JSON 列表")
    ap.add_argument("--out")
    ap.add_argument("--md")
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("DWG_JOBS", "4")))
    args = ap.parse_args(argv)

    results = [one(t, args.jobs) for t in args.truth]
    report = {"schema": "acceptance-report/1.0", "results": results,
              "summary": {"drawings": len(results),
                          "passed": sum(1 for r in results if r["ok"])}}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"acceptance -> {args.out}")
    text = markdown(results)
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"markdown -> {args.md}")
    print(text)
    return 0 if report["summary"]["passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
