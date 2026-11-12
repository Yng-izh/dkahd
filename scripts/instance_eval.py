#!/usr/bin/env python3
"""实例级验收：逐构件配对（IoU），给出 P / R / F1 —— 不看数量，看"这一根对不对"。

为什么需要它
------------
``acceptance.py`` 用的是**数量口径**（墙 31 对 31 就算过）。数量对得上不代表
对得准：会所那张图墙从 41 变成 37、艺术楼从 67 变成 58，方向不明——没有逐实例
配对，就只能猜"是算好了还是算坏了"。本脚本把"数量验收"补成"实例验收"：

* 真值里给出每个构件的**位置**（轴对齐包围盒即可）；
* 识别结果按 IoU ≥ 阈值（默认 0.5）一对一贪心匹配；
* 输出版 TP / FP / FN、Precision / Recall / F1，按类别分开。

真值必须**人工标注**，脚本不会替你生成
--------------------------------------
``--bootstrap`` 只导出"待标注草稿"（把某次识别的结果写成 gt 的空壳，坐标留着
让你改），**不能直接当基线**——那等于用识别结果给自己打分。

真值格式（``gt/<drawing>.instances.json``）
------------------------------------------
```json
{
  "schema": "instances/1.0",
  "drawing": "会所-图层混乱.dxf",
  "annotator": "人名",
  "annotated_at": "2026-11-11",
  "instances": {
    "wall":   [[x0, y0, x1, y1], ...],
    "column": [[x0, y0, x1, y1], ...],
    "door":   [[x0, y0, x1, y1], ...],
    "window": [[x0, y0, x1, y1], ...],
    "room":   [[x0, y0, x1, y1], ...]
  },
  "note": "只标这张图主体平面；图签、详图不标"
}
```

用法
----
    python3 scripts/instance_eval.py --drawing 会所-图层混乱.dxf \\
        --gt gt/会所-图层混乱.instances.json --iou 0.5 \\
        --out work/reports/instances.会所.json --md work/reports/instances.会所.md

    # 只导出草稿供人工改：
    python3 scripts/instance_eval.py --drawing X.dxf --bootstrap /tmp/X.draft.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwg_reader.pipeline import resolve_dxf                       # noqa: E402
from dwg_reader.recognition.pipeline import run_recognition       # noqa: E402

CATEGORIES = ("wall", "column", "door", "window", "room")
DEFAULT_IOU = 0.5


# --------------------------------------------------------------------------- 几何
def _bbox_of(component: dict):
    """构件 -> 轴对齐包围盒 ``[x0, y0, x1, y1]``；取不到返回 None。"""
    geom = component.get("geometry") or {}
    box = geom.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4 and all(
            isinstance(v, (int, float)) for v in box):
        return [float(v) for v in box]

    pts = []
    for key in ("outline", "polygon", "boundary", "points"):
        seq = geom.get(key)
        if isinstance(seq, (list, tuple)) and seq and isinstance(seq[0], (list, tuple)):
            pts.extend((float(p[0]), float(p[1])) for p in seq if len(p) >= 2)
    for key in ("start", "end", "center", "insert"):
        p = geom.get(key)
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            pts.append((float(p[0]), float(p[1])))
    axis = geom.get("axis")
    if isinstance(axis, (list, tuple)) and len(axis) == 2:
        pts.extend((float(p[0]), float(p[1])) for p in axis)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    box = [min(xs), min(ys), max(xs), max(ys)]
    # 墙/门/窗的几何是"轴线 + 厚度"：直接取包围盒会得到零面积（一条线），
    # IoU 恒为 0，召回会被误判成全漏。按厚度补出可见宽度再参与匹配。
    try:
        thick = float(geom.get("thickness") or 0.0)
    except (TypeError, ValueError):
        thick = 0.0
    pad = max(thick, 1.0) / 2.0
    if box[3] - box[1] <= 1e-9:
        box[1] -= pad
        box[3] += pad
    if box[2] - box[0] <= 1e-9:
        box[0] -= pad
        box[2] += pad
    return box


def _iou(a, b) -> float:
    """两个轴对齐包围盒的交并比。"""
    if not a or not b:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _match(preds, truths, threshold):
    """贪心一对一匹配：按 IoU 从高到低配对。返回 (tp, matched_pairs, fp_idx, fn_idx)。"""
    pairs = []
    for i, p in enumerate(preds):
        for j, t in enumerate(truths):
            v = _iou(p, t)
            if v >= threshold:
                pairs.append((v, i, j))
    pairs.sort(reverse=True)
    used_p, used_t, matched = set(), set(), []
    for v, i, j in pairs:
        if i in used_p or j in used_t:
            continue
        used_p.add(i)
        used_t.add(j)
        matched.append((i, j, round(v, 4)))
    fp = [i for i in range(len(preds)) if i not in used_p]
    fn = [j for j in range(len(truths)) if j not in used_t]
    return matched, fp, fn


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return round(p, 3), round(r, 3), round(f, 3)


# --------------------------------------------------------------------------- 主流程
def _predict(drawing: str, jobs: int, llm_key: str | None):
    path = resolve_dxf(drawing)
    run = run_recognition(str(path), jobs=jobs, targets=("assemble",),
                          collect_stats=True, llm_api_key=llm_key)
    comps = run.outputs["assemble"]["components"]
    by = {}
    for c in comps:
        box = _bbox_of(c)
        if box is None:
            continue
        by.setdefault(c.get("category"), []).append(box)
    return path, by, run


def _bootstrap(drawing: str, out: str, jobs: int, llm_key: str | None) -> None:
    """导出**待标注草稿**：结构齐全、坐标来自本次识别，必须人工核改后才能用。"""
    path, by, run = _predict(drawing, jobs, llm_key)
    payload = {
        "schema": "instances/1.0",
        "drawing": path.name,
        "annotator": "TODO",
        "annotated_at": "TODO",
        "instances": {k: by.get(k, []) for k in CATEGORIES},
        "note": ("草稿：坐标来自一次识别结果，**不是真值**。必须人工逐条核改"
                 "（删除误检、补上漏检、修正偏差），否则等于自己给自己打分。"),
        "draft_from_run": {"duration_ms": round(run.duration_ms, 1),
                           "total_components": sum(len(v) for v in by.values())},
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"bootstrap": out, "counts": {k: len(v) for k, v in by.items()}},
                     ensure_ascii=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="实例级验收（逐构件 IoU 匹配 → P/R/F1）")
    ap.add_argument("--drawing", required=True)
    ap.add_argument("--gt", help="人工标注实例真值 json")
    ap.add_argument("--iou", type=float, default=DEFAULT_IOU)
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("DWG_JOBS", "4")))
    ap.add_argument("--out")
    ap.add_argument("--md")
    ap.add_argument("--bootstrap", help="只导出待标注草稿（不能当基线）")
    args = ap.parse_args(argv)

    key = os.environ.get("LLM_API_KEY") or None

    if args.bootstrap:
        _bootstrap(args.drawing, args.bootstrap, args.jobs, key)
        return 0
    if not args.gt:
        ap.error("需要 --gt（人工标注实例真值）或 --bootstrap（只导出草稿）")

    spec = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    truth = spec.get("instances") or {}
    path, by, run = _predict(args.drawing, args.jobs, key)

    rows, macro = {}, []
    for cat in CATEGORIES:
        t = [list(map(float, b)) for b in (truth.get(cat) or [])]
        p = [list(map(float, b)) for b in (by.get(cat) or [])]
        if not t and not p:
            continue
        matched, fp, fn = _match(p, t, args.iou)
        tp = len(matched)
        prec, rec, f1 = _prf(tp, fp, fn)
        rows[cat] = {"truth": len(t), "pred": len(p), "tp": tp, "fp": len(fp), "fn": len(fn),
                     "precision": prec, "recall": rec, "f1": f1,
                     "matched": matched, "fp_bbox": [p[i] for i in fp],
                     "fn_bbox": [t[j] for j in fn]}
        if f1 or tp:
            macro.append(f1)

    result = {
        "schema": "instance-eval/1.0",
        "drawing": path.name,
        "gt": args.gt, "annotator": spec.get("annotator"),
        "iou_threshold": args.iou,
        "rows": rows,
        "macro_f1": round(sum(macro) / len(macro), 3) if macro else 0.0,
        "duration_ms": round(run.duration_ms, 1),
        "caveat": "不同 IoU 阈值下结论会变；墙体按包围盒匹配对细长斜墙偏严格。",
    }

    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    lines = [f"# 实例级验收 · {path.name}", "",
             f"IoU 阈值 **{args.iou}**，标注人 {spec.get('annotator') or '未写'}", "",
             "| 类别 | 真值 | 识别 | TP | FP | FN | P | R | F1 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for cat, r in rows.items():
        lines.append(f"| {cat} | {r['truth']} | {r['pred']} | {r['tp']} | {r['fp']} | "
                     f"{r['fn']} | {r['precision']} | {r['recall']} | **{r['f1']}** |")
    lines += ["", f"宏平均 F1：**{result['macro_f1']}**", "",
              "> 数量对得上不等于对得准：本表按包围盒一对一匹配，FP/FN 的具体坐标见 JSON。"]
    md = "\n".join(lines) + "\n"
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
