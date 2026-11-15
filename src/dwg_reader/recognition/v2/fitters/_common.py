"""专用拟合器共用的几何工具：只做计算，不做判定。

这里的每个函数都只回答"这块几何量出来是多少"，不回答"所以它是不是窗"。
判定留在各拟合器里（``door.py`` / ``window.py`` / ``column.py``），并且必须写在
``evidence`` 里说明依据。

尺度约定：:data:`LIMITS` 里的数都是**无量纲比例**（相对实例自身跨度/墙厚/噪声），
不存在毫米常量。一根 300 宽的窄窗框和一根 2400 宽的落地窗，用的是同一套比例。
"""
from __future__ import annotations

import math

#: 无量纲比例与离散化桶（不是毫米数，也不是"调好的阈值"）
LIMITS = {
    "hull_max_points": 4000,     # 凸包参与点数上限（预算）
    "angle_buckets": 15.0,       # 形状签名：方向离散化（度）
    "length_buckets": (0.05, 0.15, 0.3, 0.5, 0.75, 1.01),
    "pos_buckets": (-0.75, -0.25, 0.25, 0.75),   # 形状签名：相对位置离散化
    "signature_distance": 0.42,  # 归一化形状距离的上限（越像越小）
    "span_min_ratio": 0.02,      # 构件最短边 / 最长边（退化过滤）
    "host_gap_ratio": 1.5,       # 宿主墙距 / 墙厚（贴墙判定）
}


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _len(a, b) -> float:
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _angle(a, b) -> float:
    return math.degrees(math.atan2(float(b[1]) - float(a[1]),
                                   float(b[0]) - float(a[0]))) % 180.0


def _angle_diff(x, y) -> float:
    d = abs(float(x) - float(y)) % 180.0
    return min(d, 180.0 - d)


def _pt_seg_dist(p, a, b) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(float(p[0]) - ax, float(p[1]) - ay)
    t = max(0.0, min(1.0, ((float(p[0]) - ax) * dx + (float(p[1]) - ay) * dy) / L2))
    return math.hypot(float(p[0]) - (ax + t * dx), float(p[1]) - (ay + t * dy))


def _union(boxes):
    rows = [b for b in (boxes or ()) if b and len(b) == 4]
    if not rows:
        return None
    return [_r(min(b[0] for b in rows)), _r(min(b[1] for b in rows)),
            _r(max(b[2] for b in rows)), _r(max(b[3] for b in rows))]


def _box_gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _iou(a, b) -> float:
    if not a or not b:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def _median(vals) -> float:
    rows = sorted(float(v) for v in (vals or ()))
    if not rows:
        return 0.0
    return rows[len(rows) // 2]


def _quantile(vals, q: float) -> float:
    rows = sorted(float(v) for v in (vals or ()))
    if not rows:
        return 0.0
    return rows[min(len(rows) - 1, max(0, int(float(q) * (len(rows) - 1))))]


def _center(box):
    if not box:
        return [0.0, 0.0]
    return [(float(box[0]) + float(box[2])) / 2.0,
            (float(box[1]) + float(box[3])) / 2.0]


# --------------------------------------------------------------------- 图元取点
def segments_of(entity) -> list:
    """任何 kind 都拆成线段（弧用弦近似，填充用外环边），拟合器只需要线段。"""
    kind = entity.get("kind")
    geom = entity.get("geom") or {}
    out: list = []
    if kind == "segment":
        a, b = geom.get("a"), geom.get("b")
        if a and b:
            out.append([a, b])
    elif kind == "poly":
        pts = [p for p in (geom.get("pts") or ()) if p]
        for i in range(len(pts) - 1):
            out.append([pts[i], pts[i + 1]])
        if geom.get("closed") and len(pts) >= 3:
            out.append([pts[-1], pts[0]])
    elif kind == "arc":
        center, radius = geom.get("center"), float(geom.get("radius") or 0.0)
        a0 = float(geom.get("start_deg") or 0.0)
        a1 = float(geom.get("end_deg") or 0.0)
        if center and radius > 0:
            pts = []
            for i in range(9):
                ang = math.radians(a0 + (a1 - a0) * i / 8.0)
                pts.append([float(center[0]) + radius * math.cos(ang),
                            float(center[1]) + radius * math.sin(ang)])
            for i in range(len(pts) - 1):
                out.append([pts[i], pts[i + 1]])
    elif kind == "hatch":
        for loop in geom.get("loops") or ():
            pts = [p for p in (loop or ()) if p]
            for i in range(len(pts) - 1):
                out.append([pts[i], pts[i + 1]])
            if len(pts) >= 3:
                out.append([pts[-1], pts[0]])
    return out


def points_of(entities) -> list:
    pts: list = []
    for e in entities or ():
        if e.get("kind") == "text":
            continue
        for a, b in segments_of(e):
            pts.append([float(a[0]), float(a[1])])
            pts.append([float(b[0]), float(b[1])])
    return pts


def bbox_of(entities):
    return _union([e.get("bbox") for e in entities or ()])


def is_closed(entity) -> bool:
    geom = entity.get("geom") or {}
    if entity.get("kind") == "poly":
        return bool(geom.get("closed"))
    if entity.get("kind") == "hatch":
        return True
    return False


# --------------------------------------------------------------------- 最小外接矩形
def hull(points) -> list:
    rows = sorted({(round(float(p[0]), 4), round(float(p[1]), 4))
                   for p in (points or ())})[:int(LIMITS["hull_max_points"])]
    if len(rows) <= 2:
        return [[p[0], p[1]] for p in rows]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for pt in rows:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], pt) <= 0:
            lower.pop()
        lower.append(pt)
    upper: list = []
    for pt in reversed(rows):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], pt) <= 0:
            upper.pop()
        upper.append(pt)
    return [[p[0], p[1]] for p in (lower[:-1] + upper[:-1])]


def min_area_rect(points) -> dict | None:
    """最小外接矩形：长、宽、长边方向、中心、四角。柱的旋转与长宽比都从这里来。"""
    h = hull(points)
    if len(h) < 2:
        return None
    best = None
    for i in range(len(h)):
        a, b = h[i], h[(i + 1) % len(h)]
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        ca, sa = math.cos(-ang), math.sin(-ang)
        xs = [p[0] * ca - p[1] * sa for p in h]
        ys = [p[0] * sa + p[1] * ca for p in h]
        w, height = max(xs) - min(xs), max(ys) - min(ys)
        area = w * height
        if best is None or area < best["area"] - 1e-9:
            cx, cy = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
            cb, sb = math.cos(ang), math.sin(ang)
            best = {"w": w, "h": height, "area": area,
                    "angle": math.degrees(ang) % 180.0,
                    "center": [cx * cb - cy * sb, cx * sb + cy * cb]}
    if best is None:
        return None
    ang = math.radians(best["angle"])
    ca, sa = math.cos(ang), math.sin(ang)
    hw, hh = best["w"] / 2.0, best["h"] / 2.0
    cx, cy = best["center"]
    best["outline"] = [[_r(cx + dx * ca - dy * sa), _r(cy + dx * sa + dy * ca)]
                       for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
    best["center"] = [_r(cx), _r(cy)]
    best["w"] = _r(best["w"])
    best["h"] = _r(best["h"])
    best["area"] = _r(best["area"])
    best["angle"] = _r(best["angle"], 2)
    return best


def axis_from(points):
    """构件的长轴（门窗的开口线、柱的主方向）。"""
    rect = min_area_rect(points)
    if not rect:
        return None
    ang = math.radians(rect["angle"])
    half = rect["w"] / 2.0
    if rect["h"] > rect["w"]:
        ang += math.pi / 2.0
        half = rect["h"] / 2.0
    cx, cy = rect["center"]
    dx, dy = math.cos(ang), math.sin(ang)
    return [[_r(cx - dx * half), _r(cy - dy * half)],
            [_r(cx + dx * half), _r(cy + dy * half)]]


# --------------------------------------------------------------------- 局部尺度
def local_scales(entities, basis) -> dict:
    """按**实例自身**估计尺度，而不是拿全图墙厚当唯一门槛。

    一扇窄窗框的框间距可能只有全图墙厚的几分之一；用全局墙厚做带会把这类窗直
    接排除。这里给的量全部可在 ``evidence`` 里复查。
    """
    lengths = [float(e.get("length") or 0.0) for e in entities or ()
               if float(e.get("length") or 0.0) > 0]
    gaps = [abs(float(v)) for v in (basis.get("_gaps") or ())]
    rect = min_area_rect(points_of(entities))
    span = max(rect["w"], rect["h"]) if rect else 0.0
    depth = min(rect["w"], rect["h"]) if rect else 0.0
    noise = float(basis.get("noise") or 1.0)
    return {"span": _r(span), "depth": _r(depth),
            "short": _r(_quantile(lengths, 0.2)),
            "median": _r(_median(lengths)),
            "longest": _r(max(lengths) if lengths else 0.0),
            "frame_gap": _r(_median(gaps)),
            "noise": _r(noise),
            "wall": _r(float(basis.get("thickness") or 200.0)),
            "entity_count": len(entities or ())}


# --------------------------------------------------------------------- 形状签名
def _bucket_length(ratio: float) -> int:
    for idx, edge in enumerate(LIMITS["length_buckets"]):
        if ratio <= float(edge):
            return idx
    return len(LIMITS["length_buckets"])


def _bucket_pos(ratio: float) -> int:
    for idx, edge in enumerate(LIMITS["pos_buckets"]):
        if ratio <= float(edge):
            return idx
    return len(LIMITS["pos_buckets"])


def shape_signature(entities, *, mirror: bool = False) -> dict | None:
    """旋转/缩放/镜像不变的形状签名（离散桶）。

    做法：把实例里的线段搬到**实例自身坐标系**（原点=外接矩形中心，X 轴=长边），
    除以长边长度后离散化。这样同一扇窗在别处被旋转、镜像、缩放，签名仍然一致。
    """
    pts = points_of(entities)
    if len(pts) < 2:
        return None
    rect = min_area_rect(pts)
    if not rect or rect["w"] <= 0 or rect["h"] <= 0:
        return None
    ang = math.radians(rect["angle"])
    ca, sa = math.cos(-ang), math.sin(-ang)
    scale = max(rect["w"], rect["h"]) or 1.0
    cx, cy = rect["center"]
    flip = -1.0 if mirror else 1.0
    kinds: dict = {}
    rows: list = []
    for e in entities or ():
        if e.get("kind") == "text":
            continue
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
        for a, b in segments_of(e):
            la = [((a[0] - cx) * ca - (a[1] - cy) * sa) / scale * 1.0,
                  ((a[0] - cx) * sa + (a[1] - cy) * ca) / scale * flip]
            lb = [((b[0] - cx) * ca - (b[1] - cy) * sa) / scale * 1.0,
                  ((b[0] - cx) * sa + (b[1] - cy) * ca) / scale * flip]
            length = _len(la, lb) / scale
            direction = _angle_diff(_angle(la, lb), 0.0)
            mid = [(la[0] + lb[0]) / 2.0, (la[1] + lb[1]) / 2.0]
            rows.append((_bucket_length(length),
                         int(round(direction / float(LIMITS["angle_buckets"]))),
                         _bucket_pos(mid[0]), _bucket_pos(mid[1])))
    return {"kinds": dict(sorted(kinds.items())), "rows": sorted(rows),
            "aspect": _r(rect["w"] / max(1e-9, rect["h"]), 3),
            "n": len([e for e in entities or () if e.get("kind") != "text"])}


def signature_distance(a, b) -> float:
    """两个签名的归一化距离（0=一样，1=毫无共同点）。"""
    if not a or not b:
        return 1.0
    total = max(1, int(a.get("n") or 0), int(b.get("n") or 0))
    keys = set(a.get("kinds") or ()) | set(b.get("kinds") or ())
    kinds = sum(abs((a.get("kinds") or {}).get(k, 0) - (b.get("kinds") or {}).get(k, 0))
                for k in keys) / float(total)
    sa, sb = set(map(tuple, a.get("rows") or ())), set(map(tuple, b.get("rows") or ()))
    union = len(sa | sb) or 1
    rows = 1.0 - len(sa & sb) / float(union)
    aspect = abs(float(a.get("aspect") or 1.0) - float(b.get("aspect") or 1.0)) / \
        max(1e-9, max(float(a.get("aspect") or 1.0), float(b.get("aspect") or 1.0)))
    return _r(min(1.0, 0.4 * kinds + 0.4 * rows + 0.2 * min(1.0, aspect)), 3)


def best_signature_distance(ref, signature_fn) -> tuple[float, bool]:
    """签名比较允许镜像：返回 ``(最小距离, 是否用了镜像)``。"""
    if not ref:
        return 1.0, False
    plain = signature_fn(False)
    flipped = signature_fn(True)
    d0, d1 = signature_distance(ref, plain), signature_distance(ref, flipped)
    return (d1, True) if d1 < d0 else (d0, False)


# --------------------------------------------------------------------- 上下文
def wall_context(walls, box, *, tol: float) -> dict:
    """最近的墙，以及它跟目标是不是"贴着/穿过"（宿主判定只看几何）。"""
    if not box:
        return {"hit": False, "reason": "NO_BOX"}
    center = _center(box)
    best = None
    for wall in walls or ():
        if not isinstance(wall, dict):
            continue
        a, b = wall.get("start"), wall.get("end")
        if not a or not b:
            continue
        gap = _pt_seg_dist(center, a, b)
        if best is None or gap < best["gap"]:
            best = {"gap": gap, "wall": wall.get("id"),
                    "axis": [[_r(a[0]), _r(a[1])], [_r(b[0]), _r(b[1])]],
                    "thickness": _r(wall.get("thickness")),
                    "direction": _r(_angle(a, b), 2)}
    if best is None:
        # 没有墙就必须说"没有"，不能把距离当 0——否则模型会以为它就在墙上
        return {"hit": False, "reason": "NO_WALL_GIVEN", "gap": None,
                "thickness": None, "band_hit": False, "on_axis": False}
    half = 0.5 * float(best.get("thickness") or 0.0)
    band = float(best.get("thickness") or 0.0) * float(LIMITS["host_gap_ratio"])
    gap = float(best["gap"])
    return dict(best, gap=gap,
                band_hit=bool(gap <= max(tol, band)),
                on_axis=bool(half > 0 and gap <= half),
                hit=bool(gap <= max(tol, band)))


def grid_context(grid, box, *, tol: float) -> dict:
    """轴网上下文：目标是否落在轴线上/轴线交点附近（结构位置证据）。"""
    if not box or not isinstance(grid, dict):
        return {"axes": [], "intersection": False}
    center = _center(box)
    hit: list = []
    for axis in grid.get("axes") or ():
        line = (axis or {}).get("axis") or []
        if len(line) >= 2 and _pt_seg_dist(center, line[0], line[1]) <= tol:
            hit.append({"label": axis.get("label"), "orient": axis.get("orient"),
                        "offset": _r(_pt_seg_dist(center, line[0], line[1]))})
    near = False
    for cross in grid.get("intersections") or ():
        pt = (cross or {}).get("point")
        if pt and _len(center, pt) <= tol:
            near = True
            break
    return {"axes": hit[:4], "intersection": near}


def contour_edges(entities) -> dict:
    """把一群图元按"平行/垂直"分组（重构成矩形柱要用）。"""
    segs = []
    for e in entities or ():
        for a, b in segments_of(e):
            if _len(a, b) > 0:
                segs.append({"a": a, "b": b, "angle": _angle(a, b), "len": _len(a, b)})
    groups: list = []
    for seg in sorted(segs, key=lambda s: -s["len"]):
        for group in groups:
            if _angle_diff(group["angle"], seg["angle"]) <= 2.0:
                group["segs"].append(seg)
                break
        else:
            groups.append({"angle": seg["angle"], "segs": [seg]})
    return {"segments": segs, "groups": groups}


# --------------------------------------------------------------------- 近邻索引
def near_index(entities, *, cell: float) -> dict:
    """按格网索引图元（只为了"看旁边有什么"，不做判定）。"""
    grid: dict = {}
    for e in entities or ():
        box = e.get("bbox")
        if not box:
            continue
        for gx in range(int(float(box[0]) // cell), int(float(box[2]) // cell) + 1):
            for gy in range(int(float(box[1]) // cell), int(float(box[3]) // cell) + 1):
                grid.setdefault((gx, gy), []).append(e)
    return {"cell": float(cell), "grid": grid}


def near_entities(index, box, *, pad: float = 0.0) -> list:
    if not index or not box:
        return []
    cell = float(index.get("cell") or 1.0)
    area = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
    seen: dict = {}
    for gx in range(int(area[0] // cell), int(area[2] // cell) + 1):
        for gy in range(int(area[1] // cell), int(area[3] // cell) + 1):
            for e in (index.get("grid") or {}).get((gx, gy), ()):
                seen[e.get("uid")] = e
    return list(seen.values())
