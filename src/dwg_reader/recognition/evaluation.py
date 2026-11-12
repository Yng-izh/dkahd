"""跨图纸评估：真值集 -> 三层指标（候选召回 / 最终精度 / 几何拓扑）。

为什么单独一个模块：``PROBLEMS.md`` 那种"连续墙 31 / 房间 21 / confirmed 61.9%"
只是**自身口径的统计**，它无法回答"这条规则到底提升了准确率，还是只是把数字
改大了"。判断泛化必须对着独立标注算 Precision / Recall，而且要分层看：

=========== ============================ ===============================
层次        指标                          要回答的问题
=========== ============================ ===============================
候选生成     candidate recall             真构件有没有进入候选池
最终识别     precision / recall / F1      最终输出对不对
几何与拓扑   墙长覆盖 / 房间 IoU / 拓扑     位置与连接对不对
=========== ============================ ===============================

``confirmed`` 比例只作为**状态指标**单独统计，不参与 Precision 计算——把
"自己确认自己"的占比当准确率是自证。

真值文件格式见 :data:`GT_SCHEMA` / :func:`normalize_gt`。用草稿真值
（``draft: true``，由 :func:`bootstrap` 从一次识别结果导出的人工校核起点）
算出来的精度必须显式标注，不能当基线对外引用。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

GT_SCHEMA = "ground-truth/1.0"
REPORT_SCHEMA = "eval-report/1.0"

#: 墙体配对：允许的厚度误差（mm）与沿墙向重叠比例
WALL_OFFSET_TOL = 150.0
WALL_OVERLAP = 0.50
WALL_ANGLE_TOL = 12.0
#: 柱：中心距容差 = max(该值, 0.6 * 短边)
COL_CENTER_TOL = 150.0
#: 门窗：中心距与宽度误差
OPENING_CENTER_TOL = 400.0
OPENING_WIDTH_TOL = 0.35
#: 房间：IoU 命中阈值
ROOM_IOU = 0.50


# --------------------------------------------------------------------------- 真值
def normalize_gt(raw: dict) -> dict:
    """真值规范化（缺字段补空、数值转 float），并校验 schema。"""
    if not isinstance(raw, dict):
        raise ValueError("真值必须是 JSON 对象")
    schema = raw.get("schema") or GT_SCHEMA
    if not str(schema).startswith("ground-truth/"):
        raise ValueError(f"未知真值 schema: {schema}")
    out = {"schema": GT_SCHEMA, "drawing": raw.get("drawing"),
           "draft": bool(raw.get("draft", False)),
           "units": raw.get("units") or "mm",
           "regions": list(raw.get("regions") or ()),
           "walls": [], "columns": [], "doors": [], "windows": [], "rooms": []}
    for w in raw.get("walls") or ():
        s, e = w.get("start"), w.get("end")
        if not s or not e:
            continue
        out["walls"].append({"id": w.get("id"), "start": [float(s[0]), float(s[1])],
                             "end": [float(e[0]), float(e[1])],
                             "thickness": float(w.get("thickness") or 0.0) or None,
                             "region": w.get("region")})
    for key in ("columns", "doors", "windows"):
        for item in raw.get(key) or ():
            rec = dict(item)
            for field in ("center", "bbox"):
                if rec.get(field):
                    rec[field] = [float(v) for v in rec[field]]
            out[key].append(rec)
    for r in raw.get("rooms") or ():
        poly = [[float(p[0]), float(p[1])] for p in (r.get("polygon") or ())]
        if len(poly) < 3:
            continue
        out["rooms"].append({"id": r.get("id"), "polygon": poly,
                             "name": r.get("name"),
                             "area_m2": (float(r["area_m2"])
                                         if r.get("area_m2") else None)})
    return out


def load_gt(path) -> dict:
    return normalize_gt(json.loads(Path(path).read_text(encoding="utf-8")))


def save_gt(gt: dict, path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- 几何
def _seg(points):
    """兼容三种写法：``[[x,y],[x,y]]`` / ``{"start","end"}`` / 已归一化的段。"""
    if isinstance(points, dict):
        points = [points.get("start"), points.get("end")]
    if not points or len(points) < 2 or not points[0] or not points[1]:
        return None
    return ([float(points[0][0]), float(points[0][1])],
            [float(points[1][0]), float(points[1][1])])


def _seg_len(seg) -> float:
    return math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1])


def _angle(seg) -> float:
    return math.degrees(math.atan2(seg[1][1] - seg[0][1],
                                   seg[1][0] - seg[0][0])) % 180.0


def _angle_diff(a, b) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _bbox_center(b):
    return (float(b[0]) + float(b[2])) / 2.0, (float(b[1]) + float(b[3])) / 2.0


def _polygon_area(poly) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = float(poly[i][0]), float(poly[i][1])
        x2, y2 = float(poly[(i + 1) % n][0]), float(poly[(i + 1) % n][1])
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _point_in_poly(x, y, poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = float(poly[i][0]), float(poly[i][1])
        x2, y2 = float(poly[(i + 1) % n][0]), float(poly[(i + 1) % n][1])
        if (y1 > y) != (y2 > y):
            if x < x1 + (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9):
                inside = not inside
    return inside


def polygon_iou(a, b, samples: int = 40) -> float:
    """栅格采样 IoU（不引入几何裁剪库，结果稳定可比）。"""
    if not a or not b:
        return 0.0
    xs = [float(p[0]) for p in list(a) + list(b)]
    ys = [float(p[1]) for p in list(a) + list(b)]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return 0.0
    step_x, step_y = (x1 - x0) / samples, (y1 - y0) / samples
    inter = union = 0
    for i in range(samples):
        x = x0 + (i + 0.5) * step_x
        for j in range(samples):
            y = y0 + (j + 0.5) * step_y
            ia, ib = _point_in_poly(x, y, a), _point_in_poly(x, y, b)
            if ia or ib:
                union += 1
                if ia and ib:
                    inter += 1
    return inter / union if union else 0.0


def _wall_overlap(g, p) -> tuple[float, float]:
    """gt 墙被 pred 墙覆盖的比例 + pred 相对 gt 轴的法向偏移。"""
    gs, pe = _seg(g), _seg(p)
    if gs is None or pe is None:
        return 0.0, 1e9
    gl, pl = _seg_len(gs), _seg_len(pe)
    if gl <= 0 or pl <= 0:
        return 0.0, 1e9
    if _angle_diff(_angle(gs), _angle(pe)) > WALL_ANGLE_TOL:
        return 0.0, 1e9
    ux, uy = (gs[1][0] - gs[0][0]) / gl, (gs[1][1] - gs[0][1]) / gl
    nx, ny = -uy, ux
    offs = [abs((q[0] - gs[0][0]) * nx + (q[1] - gs[0][1]) * ny) for q in pe]
    offset = (offs[0] + offs[1]) / 2.0
    proj = sorted((q[0] - gs[0][0]) * ux + (q[1] - gs[0][1]) * uy for q in pe)
    lo, hi = max(0.0, proj[0]), min(gl, proj[1])
    return max(0.0, hi - lo) / gl, offset


def _match_walls(gt_walls, pred_walls):
    used = set()
    pairs = []
    for gi, g in enumerate(gt_walls):
        best = None
        gt_t = float(g.get("thickness") or 0.0)
        for pi, p in enumerate(pred_walls):
            if pi in used:
                continue
            ov, off = _wall_overlap(g, p)
            if ov < WALL_OVERLAP:
                continue
            p_t = float(p.get("thickness") or 0.0) if isinstance(p, dict) else 0.0
            tol = max(WALL_OFFSET_TOL, 0.6 * max(gt_t, p_t))
            if off > tol:
                continue
            if best is None or ov > best[0]:
                best = (ov, pi)
        if best is None:
            pairs.append((gi, None))
            continue
        used.add(best[1])
        pairs.append((gi, best[1]))
    unmatched_pred = [i for i in range(len(pred_walls)) if i not in used]
    return pairs, unmatched_pred


def _match_boxes(gt_items, pred_items, tol=COL_CENTER_TOL):
    used, pairs = set(), []
    for gi, g in enumerate(gt_items):
        gc = _item_center(g)
        if gc is None:
            pairs.append((gi, None))
            continue
        best = None
        for pi, p in enumerate(pred_items):
            if pi in used:
                continue
            pc = _item_center(p)
            if pc is None:
                continue
            short = 0.0
            pb = p.get("bbox")
            if pb:
                short = min(pb[2] - pb[0], pb[3] - pb[1])
            limit = max(tol, 0.6 * short)
            d = math.hypot(gc[0] - pc[0], gc[1] - pc[1])
            if d <= limit and (best is None or d < best[0]):
                best = (d, pi)
        if best is None:
            pairs.append((gi, None))
            continue
        used.add(best[1])
        pairs.append((gi, best[1]))
    return pairs, [i for i in range(len(pred_items)) if i not in used]


def _match_openings(gt_items, pred_items, tol=OPENING_CENTER_TOL):
    used, pairs = set(), []
    for gi, g in enumerate(gt_items):
        gc = _item_center(g)
        gw = float(g.get("width") or 0.0)
        best = None
        for pi, p in enumerate(pred_items):
            if pi in used:
                continue
            pc = _item_center(p)
            if pc is None:
                continue
            d = math.hypot(gc[0] - pc[0], gc[1] - pc[1])
            if d > tol:
                continue
            pw = float(p.get("width") or 0.0)
            if gw and pw and abs(pw - gw) > OPENING_WIDTH_TOL * max(gw, pw):
                continue
            if best is None or d < best[0]:
                best = (d, pi)
        if best is None:
            pairs.append((gi, None))
            continue
        used.add(best[1])
        pairs.append((gi, best[1]))
    return pairs, [i for i in range(len(pred_items)) if i not in used]


def _match_rooms(gt_rooms, pred_rooms, threshold=ROOM_IOU):
    used, pairs = set(), []
    for gi, g in enumerate(gt_rooms):
        best = None
        for pi, p in enumerate(pred_rooms):
            if pi in used:
                continue
            iou = polygon_iou(g["polygon"], p["polygon"])
            if iou < threshold:
                continue
            if best is None or iou > best[0]:
                best = (iou, pi)
        if best is None:
            pairs.append((gi, None))
            continue
        used.add(best[1])
        pairs.append((gi, best[1]))
    return pairs, [i for i in range(len(pred_rooms)) if i not in used]


# --------------------------------------------------------------------------- 预测
def _item_center(item):
    if item.get("center"):
        c = item["center"]
        return float(c[0]), float(c[1])
    if item.get("insert"):
        c = item["insert"]
        return float(c[0]), float(c[1])
    b = item.get("bbox")
    if b:
        return _bbox_center(b)
    return None


def pred_wall(comp):
    geo = comp.get("geometry") or {}
    seg = _seg([geo.get("start"), geo.get("end")]) if geo.get("start") and geo.get("end") else None
    return {"id": comp.get("id"), "seg": seg,
            "thickness": geo.get("thickness"),
            "region": comp.get("region")}


def pred_room(comp):
    geo = comp.get("geometry") or {}
    poly = geo.get("outline") or geo.get("polygon") or ()
    return {"id": comp.get("id"), "polygon": [[float(p[0]), float(p[1])] for p in poly],
            "name": (comp.get("attributes") or {}).get("room_name")}


def index_components(components):
    """构件列表 -> 归一化的预测集合（墙壁段 / 柱中心 / 洞口中心 / 房间多边形）。"""
    out = {"walls": [], "columns": [], "doors": [], "windows": [], "rooms": []}
    _ALIAS = {"column": "columns", "door": "doors", "window": "windows"}
    for comp in components or ():
        cat = comp.get("category")
        if cat == "wall":
            out["walls"].append(pred_wall(comp))
        elif cat == "room":
            room = pred_room(comp)
            if len(room["polygon"]) >= 3:
                out["rooms"].append(room)
        elif cat in ("column", "door", "window"):
            geo = comp.get("geometry") or {}
            attrs = comp.get("attributes") or {}
            out[_ALIAS[cat]].append({"id": comp.get("id"),
                             "center": geo.get("center") or geo.get("insert"),
                             "bbox": geo.get("bbox"),
                             "width": geo.get("width") or attrs.get("width_mm")})
    return out


def _prf(matched, n_pred, n_gt):
    p = matched / n_pred if n_pred else None
    r = matched / n_gt if n_gt else None
    if p is None or r is None or (p + r) <= 0:
        f1 = None
    else:
        f1 = 2 * p * r / (p + r)
    return {"precision": None if p is None else round(p, 4),
            "recall": None if r is None else round(r, 4),
            "f1": None if f1 is None else round(f1, 4),
            "pred": n_pred, "gt": n_gt, "matched": matched}


# --------------------------------------------------------------------------- 打分
def score(components, gt, *, candidates=None, status=None, topology=None,
          allow_draft=False, drawing=None) -> dict:
    """对一张图纸算三层指标。``candidates`` 给候选池（用于候选召回层）。"""
    gt = normalize_gt(gt)
    pred = index_components(components)
    report = {"schema": REPORT_SCHEMA,
              "drawing": drawing or gt.get("drawing"),
              "gt_status": ("draft" if gt.get("draft") else "verified"),
              "gt_draft": bool(gt.get("draft")), "final": {}, "candidate": {},
              "geometry": {}, "status": {}, "topology": dict(topology or {})}
    if gt.get("draft") and not allow_draft:
        report["error"] = "GT_IS_DRAFT: 草稿真值不能当准确率基线（用 allow_draft=True）"

    matchers = {
        "walls": lambda g, p: _match_walls([x for x in g], [x["seg"] for x in p]),
        "columns": _match_boxes, "doors": _match_openings,
        "windows": _match_openings, "rooms": _match_rooms,
    }
    gt_sets = {"walls": gt["walls"], "columns": gt["columns"], "doors": gt["doors"],
               "windows": gt["windows"], "rooms": gt["rooms"]}
    for cat, match in matchers.items():
        gt_items, pred_items = gt_sets[cat], pred[cat]
        pairs, extra = match(gt_items, pred_items)
        matched = sum(1 for _g, p in pairs if p is not None)
        report["final"][cat] = _prf(matched, len(pred_items), len(gt_items))

    # 候选召回：真构件是否进入候选池（不要求最终被接受）
    if candidates:
        pool = index_components(candidates if isinstance(candidates, list)
                                else candidates.get("components") or ())
        for cat in ("walls", "columns", "doors", "windows"):
            gt_items = gt_sets[cat]
            if not gt_items:
                continue
            pairs, _extra = matchers[cat](gt_items, pool[cat])
            hit = sum(1 for _g, p in pairs if p is not None)
            report["candidate"][cat] = {"recall": round(hit / len(gt_items), 4),
                                        "gt": len(gt_items), "hit": hit,
                                        "pool": len(pool[cat])}

    # 几何与拓扑：墙长覆盖 / 厚度误差 / 房间 IoU / 面积误差
    if gt["walls"]:
        pairs, _extra = _match_walls(gt["walls"], [w["seg"] for w in pred["walls"] if w["seg"]])
        covered = 0.0
        total = 0.0
        t_err = []
        for gi, pi in pairs:
            g = gt["walls"][gi]
            total += _seg_len(_seg([g["start"], g["end"]]))
            if pi is None:
                continue
            p = [w for w in pred["walls"] if w["seg"]][pi]
            ov, _off = _wall_overlap(g, p["seg"])
            covered += ov * _seg_len(_seg([g["start"], g["end"]]))
            if g.get("thickness") and p.get("thickness"):
                t_err.append(abs(float(p["thickness"]) - float(g["thickness"])))
        report["geometry"]["wall_length_coverage"] = (
            round(covered / total, 4) if total else None)
        report["geometry"]["wall_thickness_mae"] = (
            round(sum(t_err) / len(t_err), 1) if t_err else None)
    if gt["rooms"]:
        pairs, _extra = _match_rooms(gt["rooms"], pred["rooms"])
        ious, aerr = [], []
        for gi, pi in pairs:
            if pi is None:
                continue
            g = gt["rooms"][gi]
            p = pred["rooms"][pi]
            ious.append(polygon_iou(g["polygon"], p["polygon"]))
            if g.get("area_m2"):
                aerr.append(abs(_polygon_area(p["polygon"]) / 1e6 - g["area_m2"]))
        report["geometry"]["room_iou_mean"] = round(sum(ious) / len(ious), 4) if ious else None
        report["geometry"]["room_area_mae_m2"] = (
            round(sum(aerr) / len(aerr), 2) if aerr else None)
        report["geometry"]["room_name_hit"] = sum(
            1 for gi, pi in pairs
            if pi is not None and gt["rooms"][gi].get("name")
            and gt["rooms"][gi].get("name") == pred["rooms"][pi].get("name"))
    if status:
        report["status"] = dict(status)
    return report


def macro(reports) -> dict:
    """多张图纸宏平均（按图纸分组，避免同一张图的裁剪区域污染测试集）。"""
    cats = ("walls", "columns", "doors", "windows", "rooms")
    out = {"schema": "eval-macro/1.0", "drawings": [],
           "categories": {}, "geometry": {}}
    live = [r for r in reports or () if not r.get("error")]
    for r in reports or ():
        out["drawings"].append({"drawing": r.get("drawing"),
                                "gt_status": r.get("gt_status"),
                                "error": r.get("error")})
    for cat in cats:
        f1s = [(r["final"].get(cat) or {}).get("f1") for r in live]
        ps = [(r["final"].get(cat) or {}).get("precision") for r in live]
        rs = [(r["final"].get(cat) or {}).get("recall") for r in live]
        out["categories"][cat] = {
            "macro_f1": round(sum(x for x in f1s if x is not None)
                              / max(1, len([x for x in f1s if x is not None])), 4)
            if any(x is not None for x in f1s) else None,
            "macro_precision": round(sum(x for x in ps if x is not None)
                                     / max(1, len([x for x in ps if x is not None])), 4)
            if any(x is not None for x in ps) else None,
            "macro_recall": round(sum(x for x in rs if x is not None)
                                  / max(1, len([x for x in rs if x is not None])), 4)
            if any(x is not None for x in rs) else None}
    for key in ("wall_length_coverage", "wall_thickness_mae",
                "room_iou_mean", "room_area_mae_m2"):
        vals = [(r.get("geometry") or {}).get(key) for r in live]
        vals = [v for v in vals if v is not None]
        out["geometry"][key] = round(sum(vals) / len(vals), 4) if vals else None
    return out


# --------------------------------------------------------------------------- 草稿
def bootstrap(components, *, drawing=None, regions=(), note="待人工校核"):
    """从一次识别结果导出**草稿真值**供人工校核（不是自动真值）。"""
    pred = index_components(components)
    gt = {"schema": GT_SCHEMA, "drawing": drawing, "draft": True, "units": "mm",
          "note": note, "regions": [dict(r) for r in regions or ()],
          "walls": [], "columns": [], "doors": [], "windows": [], "rooms": []}
    for i, w in enumerate(pred["walls"], 1):
        if not w["seg"]:
            continue
        gt["walls"].append({"id": f"W{i:03d}", "start": [_r(w["seg"][0][0]), _r(w["seg"][0][1])],
                            "end": [_r(w["seg"][1][0]), _r(w["seg"][1][1])],
                            "thickness": w.get("thickness")})
    for cat in ("columns", "doors", "windows"):
        for i, item in enumerate(pred[cat], 1):
            rec = {"id": f"{cat[0].upper()}{i:03d}"}
            if item.get("center"):
                rec["center"] = [_r(item["center"][0]), _r(item["center"][1])]
            if item.get("bbox"):
                rec["bbox"] = [_r(v) for v in item["bbox"]]
            if item.get("width"):
                rec["width"] = _r(item["width"])
            gt[cat].append(rec)
    for i, room in enumerate(pred["rooms"], 1):
        gt["rooms"].append({"id": f"R{i:03d}",
                            "polygon": [[_r(p[0]), _r(p[1])] for p in room["polygon"]],
                            "name": room.get("name")})
    return gt


def _r(v, n=1):
    return round(float(v), n)


# --------------------------------------------------------------------------- 报告
def markdown(report, title="识别评估") -> str:
    """把一份（或多份）评估结果渲染成 Markdown 表。"""
    reports = report if isinstance(report, list) else [report]
    lines = [f"# {title}", ""]
    head = "| 图纸 | 真值 | " + " | ".join(
        f"{_CN[c]} P/R/F1" for c in ("walls", "columns", "doors", "windows", "rooms")) + " |"
    lines += [head, "|" + "---|" * (len(head.split("|")) - 2)]
    for r in reports:
        cells = []
        for cat in ("walls", "columns", "doors", "windows", "rooms"):
            f = (r.get("final") or {}).get(cat) or {}
            fmt = lambda v: "—" if v is None else f"{v:.3f}"
            cells.append(f"{fmt(f.get('precision'))}/{fmt(f.get('recall'))}/{fmt(f.get('f1'))}")
        lines.append(f"| {r.get('drawing')} | {r.get('gt_status')} | " + " | ".join(cells) + " |")
    lines += ["", "## 候选召回（真构件进入候选池的比例）", "",
              "| 图纸 | " + " | ".join(_CN[c] for c in ("walls", "columns", "doors", "windows")) + " |",
              "|" + "---|" * 5]
    for r in reports:
        cand = r.get("candidate") or {}
        cells = ["—" if not cand.get(c) else f"{cand[c]['recall']:.3f}"
                 for c in ("walls", "columns", "doors", "windows")]
        lines.append(f"| {r.get('drawing')} | " + " | ".join(cells) + " |")
    lines += ["", "## 几何与拓扑", "",
              "| 图纸 | 墙长覆盖 | 墙厚 MAE | 房间 IoU | 面积 MAE | 墙网分量 | 孤立墙 | 观测/推断面 |",
              "|" + "---|" * 8]
    for r in reports:
        g = r.get("geometry") or {}
        t = r.get("topology") or {}
        fmt = lambda v: "—" if v is None else str(v)
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r.get("drawing"), fmt(g.get("wall_length_coverage")),
            fmt(g.get("wall_thickness_mae")), fmt(g.get("room_iou_mean")),
            fmt(g.get("room_area_mae_m2")), fmt(t.get("wall_components")),
            fmt(t.get("wall_isolated")),
            fmt(f"{t.get('faces_observed')}/{t.get('faces_hypothesis')}")))
    lines += ["", "## 状态指标（不是准确率）", "",
              "| 图纸 | 墙 confirmed | 柱 confirmed | 门 window confirmed | 未决 |",
              "|" + "---|" * 5]
    for r in reports:
        s = r.get("status") or {}
        lines.append("| {} | {} | {} | {} | {} |".format(
            r.get("drawing"), s.get("wall_confirmed_ratio"),
            s.get("column_confirmed_ratio"), s.get("opening_confirmed_ratio"),
            s.get("undecided")))
    return "\n".join(lines) + "\n"


_CN = {"walls": "墙", "columns": "柱", "doors": "门", "windows": "窗", "rooms": "房间"}


# --------------------------------------------------------------------------- 清单评估
AI_EVAL_SCHEMA = "ai-eval/1.0"
MANIFEST_SCHEMA = "ai-eval-manifest/1.0"

#: 清单必填项（缺了就直说，不猜）
MANIFEST_KEYS = ("drawings",)

_NOTES = (
    "真值必须是**人工标注**：用基础模式自己的输出当 pseudo-label 只能测自洽，不是准确率。",
    "train/dev/test 按**项目**划分：同项目的块族会重复，按图纸随机切分会泄漏。",
    "草稿真值（draft=true）算出的数字不得作为对外基线。",
)


def run_manifest(manifest: dict, *, ai_mode: str = "off", ai_config=None,
                 run=None, limit: int | None = None) -> dict:
    """清单评估：基础模式 vs 增强模式，按项目隔离，逐类别给 P/R/F1。

    ``run(dxf_path, mode) -> {"components": [...], ...}`` 可注入（测试用）；
    缺省走 :func:`dwg_reader.recognition.pipeline.run_recognition`。
    """
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return {"schema": AI_EVAL_SCHEMA, "error": "MANIFEST_NOT_OBJECT",
                "notes": list(_NOTES)}
    for key in MANIFEST_KEYS:
        if key not in manifest:
            problems.append(f"缺字段: {key}")
    drawings = list(manifest.get("drawings") or ())
    if limit:
        drawings = drawings[:int(limit)]
    runner = run or _default_runner(ai_config)

    rows: list[dict] = []
    for item in drawings:
        if not isinstance(item, dict) or not item.get("path"):
            problems.append("drawing 条目缺少 path")
            continue
        path = str(item["path"])
        project = str(item.get("project") or manifest.get("project") or "")
        gt_path = item.get("gt")
        if not gt_path:
            problems.append(f"{path}: 没有真值文件，跳过（无真值不给数字）")
            continue
        try:
            gt = load_gt(gt_path)
        except Exception as exc:
            problems.append(f"{path}: 真值读取失败 {type(exc).__name__}")
            continue
        base = runner(path, "off")
        row = {"drawing": path, "project": project,
               "gt_status": ("draft" if gt.get("draft") else "verified"),
               "baseline": _score_components(base, gt, path),
               "ai_mode": ai_mode}
        if ai_mode != "off":
            try:
                row["enhanced"] = _score_components(runner(path, ai_mode), gt, path)
            except Exception as exc:
                row["enhanced"] = {"error": type(exc).__name__}
        rows.append(row)

    aggregate = _aggregate(rows)
    projects = sorted({r.get("project") for r in rows if r.get("project")})
    return {"schema": AI_EVAL_SCHEMA,
            "manifest": {"schema": manifest.get("schema") or MANIFEST_SCHEMA,
                         "project": manifest.get("project"),
                         "split": manifest.get("split"),
                         "drawings": len(drawings)},
            "ai_mode": ai_mode,
            "rows": rows,
            "aggregate": aggregate,
            "projects": projects,
            "project_isolated": len(projects) >= 1,
            "problems": problems,
            "notes": list(_NOTES)}


def _default_runner(ai_config):
    def run(dxf_path: str, mode: str):
        from .pipeline import run_recognition
        run_ = run_recognition(dxf_path, jobs=1, targets=("export",),
                               ai_mode=mode, ai_config=ai_config)
        return run_.output
    return run


def _score_components(result: dict, gt: dict, path: str) -> dict:
    comps = (result or {}).get("components") or ()
    rep = score(comps, gt, drawing=path, allow_draft=True)
    return {"final": rep.get("final"), "geometry": rep.get("geometry"),
            "status": rep.get("status"), "gt_draft": rep.get("gt_draft"),
            "counts": {c: sum(1 for x in comps if x.get("category") == c)
                       for c in ("wall", "column", "door", "window", "room")}}


def _aggregate(rows) -> dict:
    """汇总每个类别的匹配数（只汇总有 GT 的行；缺项不补 0，标 None）。"""
    cats = ("walls", "columns", "doors", "windows", "rooms")
    out: dict = {"baseline": {}, "enhanced": {}, "delta": {}}
    for key in ("baseline", "enhanced"):
        for cat in cats:
            pred = gt = matched = 0
            rows_n = 0
            for row in rows:
                block = (row.get(key) or {}).get("final") or {}
                cell = block.get(cat)
                if not isinstance(cell, dict) or cell.get("gt") is None:
                    continue
                pred += int(cell.get("pred") or 0)
                gt += int(cell.get("gt") or 0)
                matched += int(cell.get("matched") or 0)
                rows_n += 1
            if not rows_n:
                out[key][cat] = {"precision": None, "recall": None, "f1": None,
                                 "drawings": 0}
                continue
            out[key][cat] = dict(_prf(matched, pred, gt), drawings=rows_n)
    for cat in cats:
        b = out["baseline"].get(cat) or {}
        e = out["enhanced"].get(cat) or {}
        out["delta"][cat] = {
            "recall": _delta(b.get("recall"), e.get("recall")),
            "precision": _delta(b.get("precision"), e.get("precision")),
            "f1": _delta(b.get("f1"), e.get("f1"))}
    return out


def _delta(a, b):
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)
