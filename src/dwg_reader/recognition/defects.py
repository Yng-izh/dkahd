"""图纸缺陷诊断：把"没识别出来"拆成可修类型，并允许 VALID_OPEN_SPACE。

三类要分清：图纸表达不完整（可推断修复）、识别器不支持（补算子/语义）、
真正的绘制错误（合并、去重、告警）。
"""
from __future__ import annotations

import math

MIN_GAP, MAX_GAP = 100.0, 6000.0
COLLINEAR_TOL = 30.0
LABEL_NEAR = 4000.0

TYPES = ("BROKEN_WALL", "UNPAIRED_FACE", "WRONG_LAYER", "OPEN_CONTOUR",
         "ROOM_LEAK", "LABEL_OFFSET", "DUPLICATE_GEOMETRY", "UNSUPPORTED_PATTERN",
         "VALID_OPEN_SPACE")


def _seg(s):
    return (float(s["start"][0]), float(s["start"][1]),
            float(s["end"][0]), float(s["end"][1]))


def _collinear(a, b, tol=COLLINEAR_TOL) -> bool:
    ax, ay, ax2, ay2 = a
    bx, by, bx2, by2 = b
    dx1, dy1, dx2, dy2 = ax2 - ax, ay2 - ay, bx2 - bx, by2 - by
    l1 = math.hypot(dx1, dy1) or 1.0
    l2 = math.hypot(dx2, dy2) or 1.0
    if abs((dx1 / l1) * (dy2 / l2) - (dy1 / l1) * (dx2 / l2)) > 0.05:
        return False
    nx, ny = -dy1 / l1, dx1 / l1
    return (abs((bx - ax) * nx + (by - ay) * ny) <= tol
            and abs((bx2 - ax) * nx + (by2 - ay) * ny) <= tol)


def _gap(a, b) -> float:
    return min(math.hypot(a[2] - b[0], a[3] - b[1]), math.hypot(a[2] - b[2], a[3] - b[3]),
               math.hypot(a[0] - b[0], a[1] - b[1]), math.hypot(a[0] - b[2], a[1] - b[3]))


def _bbox2(a, b):
    xs = [a[0], a[2], b[0], b[2]]
    ys = [a[1], a[3], b[1], b[3]]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _covered(a, b, openings) -> bool:
    for op in openings or ():
        seg = op.get("opening") or op.get("axis")
        if not seg:
            continue
        p = (float(seg[0][0]), float(seg[0][1]), float(seg[1][0]), float(seg[1][1]))
        if _collinear(a, p) and _gap(a, p) < MIN_GAP * 3:
            return True
    return False


def _label_near(a, b, labels):
    cx, cy = (a[0] + a[2] + b[0] + b[2]) / 4.0, (a[1] + a[3] + b[1] + b[3]) / 4.0
    best = None
    for lab in labels or ():
        x, y = lab.get("x"), lab.get("y")
        if x is None or y is None:
            continue
        d = math.hypot(x - cx, y - cy)
        if d <= LABEL_NEAR and (best is None or d < best[0]):
            best = (d, lab)
    return best[1] if best else None


def detect(*, snapshot, prims, ledger, walls_seg=(), unpaired=(), spaces=None,
           room_labels=(), openings=(), columns=(), plan=None, limit=400) -> list[dict]:
    cases: list[dict] = []

    def add(kind, region, ids, options, detail):
        if len(cases) >= limit:
            return
        cases.append({"defect_id": f"defect_{len(cases) + 1:03d}", "type": kind,
                      "region": region, "evidence_ids": list(dict.fromkeys(ids))[:16],
                      "repair_options": options, "detail": detail})

    segs = [(w, _seg(w)) for w in walls_seg if w.get("start") and w.get("end")]
    for i, (wa, a) in enumerate(segs):
        for wb, b in segs[i + 1:]:
            if not _collinear(a, b):
                continue
            gap = _gap(a, b)
            if not (MIN_GAP <= gap <= MAX_GAP):
                continue
            if _covered(a, b, openings):
                continue
            room = _label_near(a, b, room_labels)
            ids = [f"gap:{len(cases) + 1}"]
            if room is None:
                add("VALID_OPEN_SPACE", _bbox2(a, b), ids,
                    ["option:leave_open", "option:close_gap"],
                    {"gap_mm": round(gap), "reason": "无房名依赖此闭合"})
            else:
                add("BROKEN_WALL", _bbox2(a, b), ids + [room.get("handle")],
                    ["option:close_gap", "option:opening", "option:leave_open"],
                    {"gap_mm": round(gap), "room_label": room.get("text"),
                     "wall_a": wa.get("id"), "wall_b": wb.get("id")})
                add("ROOM_LEAK", _bbox2(a, b), [room.get("handle")] + ids,
                    ["option:close_gap", "option:change_closure"],
                    {"room_label": room.get("text")})

    for item in unpaired or ():
        add("UNPAIRED_FACE", item.get("bbox"),
            [s.get("handle") for s in (item.get("srcs") or ()) if s.get("handle")],
            ["option:retry_pair", "option:single_line_wall", "option:reject"],
            {"reason": item.get("why") or "配对不足", "weak": item.get("weak")})

    for h in (prims or {}).get("hatches", ()):
        loops = h.get("loops") or ()
        if not loops:
            continue
        outer = max(loops, key=len)
        if len(outer) < 3:
            add("OPEN_CONTOUR", None, [f"ev:{h.get('handle')}"],
                ["option:rebuild_contour", "option:treat_as_fill"],
                {"loops": len(loops), "reason": "边界顶点不足"})
            continue
        closed = math.dist(outer[0], outer[-1]) <= 5.0
        if not closed:
            add("OPEN_CONTOUR", _bbox_of(outer), [f"ev:{h.get('handle')}"],
                ["option:rebuild_contour", "option:treat_as_fill", "option:leave_open"],
                {"loops": len(loops), "closed": False})

    seen = {}
    for w in walls_seg:
        key = (round(float(w.get("center") or 0)), round(float(w.get("length") or 0)))
        if key in seen:
            add("DUPLICATE_GEOMETRY", w.get("bbox"),
                [s.get("handle") for s in (w.get("srcs") or ()) if s.get("handle")],
                ["option:merge", "option:keep_thicker"], {"same_as": seen[key]})
        else:
            seen[key] = w.get("id") or w.get("orient") or "-"

    for h, rec in (ledger or {}).items():
        if rec.get("status") == "unused" and rec.get("type") in ("LWPOLYLINE", "SPLINE"):
            add("UNSUPPORTED_PATTERN", None, [f"ev:{h}"],
                ["option:llm_analyze", "option:leave_unresolved"],
                {"layer": rec.get("layer"), "type": rec.get("type")})
        if len(cases) >= limit:
            break
    return cases


def _bbox_of(pts):
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    if not xs:
        return None
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]
