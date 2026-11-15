"""门专用拟合器：平开（含双扇）、推拉、无开启弧的洞口、门联窗。

门的判据必须比"有一条弧 + 一条线"更硬，否则家具、图例、柜门都会被当成建筑门：

* **开启弧要以铰链为圆心**：弧心与门扇端点的距离要和半径对得上；
* **弧要落在墙上**：铰链离开任何墙都不能太远，洞口要能投到墙轴线上；
* **弧心半径要合门宽**：半径相对本图门宽基准过小（装饰弧、家具转角）或过大
  （弧形墙边线）都记成可疑，记成 ``uncertain`` 而不是直接删——用户要求
  "没有宿主墙但几何证据较好的门，保留为 uncertain"；
* **没有弧不等于不是门**：推拉门是两片错开的扇，洞口是两道门垛线；这两类靠
  平行扇/门垛间距 + 墙洞位置认，不靠弧。

几何只从图元算：洞口是墙轴线上的一截投影，铰链是弧心，宽度是弧半径/门垛间距。
"""
from __future__ import annotations

import math

from . import _common as C

#: 无量纲比例（相对本图门宽基准 / 墙厚 / 噪声）
LIMITS = {
    "min_radius_ratio": 0.35,      # 弧半径 / 门宽基准 的下限
    "max_radius_ratio": 2.6,       # 弧半径 / 门宽基准 的上限
    "min_sweep_deg": 45.0,         # 开启弧的圆心角下限
    "leaf_ratio": 0.22,            # 门扇长与半径的相对容差
    "hinge_tol_ratio": 0.35,       # 铰链到墙的距离 / 墙厚 的容差
    "jamb_max_ratio": 2.0,         # 门垛线长度 / 墙厚 的上限
    "sliding_ratio": 0.75,         # 推拉扇长度 / 跨度 的下限
    "double_leaf_ratio": 0.5,      # 双扇弧心距 / 半径 的下限
    "furniture_radius": 3.0,       # 半径 / 墙厚 的下限（再小更像家具圆角）
    "min_width_ratio": 0.3,        # 洞口宽 / 门宽基准 的下限
}


def _leaf_for(center, radius: float, entities, tol: float):
    """门扇：一端贴在弧心上、长度接近半径的那条线。"""
    best = None
    for e in entities or ():
        if e.get("kind") not in ("segment", "poly"):
            continue
        for a, b in C.segments_of(e):
            da, db = C._len(a, center), C._len(b, center)
            length = C._len(a, b)
            if min(da, db) > max(tol, float(LIMITS["hinge_tol_ratio"]) * radius):
                continue
            delta = abs(length - radius) / max(1e-9, radius)
            if delta > float(LIMITS["leaf_ratio"]):
                continue
            row = {"a": a, "b": b, "len": length, "delta": delta,
                   "hinge_end": a if da < db else b,
                   "free_end": b if da < db else a, "uid": e.get("uid")}
            if best is None or row["delta"] < best["delta"]:
                best = row
    return best


def _wall_hit(walls, point, *, tol: float):
    best = None
    for wall in walls or ():
        a, b = (wall or {}).get("start"), (wall or {}).get("end")
        if not a or not b:
            continue
        gap = C._pt_seg_dist(point, a, b)
        if best is None or gap < best["gap"]:
            best = {"gap": gap, "wall": wall.get("id"),
                    "axis": [[C._r(a[0]), C._r(a[1])], [C._r(b[0]), C._r(b[1])]],
                    "direction": C._r(C._angle(a, b), 2),
                    "thickness": C._r(wall.get("thickness"))}
    if best is None:
        return {"hit": False, "reason": "NO_WALL_GIVEN"}
    return dict(best, hit=bool(best["gap"] <= max(tol, float(
        best.get("thickness") or 0.0) * float(LIMITS["hinge_tol_ratio"]))))


def _project(point, axis):
    """点在墙轴线上的投影（洞口端点要落在墙上，而不是弧上）。"""
    a, b = axis[0], axis[1]
    ang = math.radians(C._angle(a, b))
    dx, dy = math.cos(ang), math.sin(ang)
    t = (point[0] - a[0]) * dx + (point[1] - a[1]) * dy
    return [C._r(a[0] + dx * t), C._r(a[1] + dy * t)]


def fit_instance(instance, *, entities, walls=(), grid=None, basis=None,
                 family=None, siblings=(), neighbors=()) -> dict | None:
    """一扇门（或"疑似门"）的几何与证据。"""
    entities = [e for e in (entities or ()) if e.get("kind") != "text"]
    if not entities:
        return None
    basis = dict(basis or {})
    scales = C.local_scales(entities, basis)
    thickness = float(scales["wall"] or 200.0)
    door_w = float(basis.get("door_w") or thickness * 4.0)
    box = C.bbox_of(entities)
    arcs = [e for e in entities if e.get("kind") == "arc"]
    points = C.points_of(entities)
    rect = C.min_area_rect(points)
    how: list = []
    negatives: list = []
    subtype = "door_unclassified"
    geometry: dict = {"bbox": box}
    hinge = opening = leaf = None
    radius = sweep = 0.0

    if arcs:
        # ---- 平开门（含双扇）：弧心=铰链，半径=门宽
        arc = max(arcs, key=lambda e: float((e.get("geom") or {}).get("radius") or 0.0))
        geom = arc.get("geom") or {}
        radius = float(geom.get("radius") or 0.0)
        sweep = abs(float(geom.get("end_deg") or 0.0)
                    - float(geom.get("start_deg") or 0.0)) % 360.0
        hinge = geom.get("center")
        leaf = _leaf_for(hinge, radius, entities, tol=scales["noise"] * 3.0) if hinge else None
        near = _wall_hit(walls, hinge, tol=scales["noise"] * 3.0) if hinge else {"hit": False}
        if hinge and near.get("hit"):
            opening = [_project(hinge, near["axis"]),
                       _project(leaf["free_end"] if leaf else
                                [hinge[0] + radius, hinge[1]], near["axis"])]
        subtype = "swing_door"
        how.append({"rule": "ARC_WITH_HINGE", "radius_mm": C._r(radius),
                    "sweep_deg": C._r(sweep)})
        if len(arcs) >= 2:
            centers = [(a.get("geom") or {}).get("center") for a in arcs[:2]]
            if centers[0] and centers[1]:
                separation = C._len(centers[0], centers[1])
                if separation >= float(LIMITS["double_leaf_ratio"]) * radius:
                    subtype = "double_leaf_door"
                    how.append({"rule": "TWO_ARCS", "separation_mm": C._r(separation)})
        if radius < float(LIMITS["min_radius_ratio"]) * door_w:
            negatives.append({"code": "ARC_TOO_SMALL",
                              "detail": C._r(radius / max(1e-9, door_w), 2)})
        if radius > float(LIMITS["max_radius_ratio"]) * door_w:
            negatives.append({"code": "ARC_TOO_LARGE",
                              "detail": C._r(radius / max(1e-9, door_w), 2)})
        if sweep < float(LIMITS["min_sweep_deg"]):
            negatives.append({"code": "ARC_SWEEP_TOO_SMALL", "detail": C._r(sweep)})
        if not leaf:
            negatives.append({"code": "LEAF_NOT_FOUND"})
        if not near.get("hit"):
            negatives.append({"code": "NO_HOST_WALL",
                              "detail": None if near.get("gap") is None
                              else C._r(near.get("gap"))})
        geometry.update({"hinge": hinge, "radius_mm": C._r(radius),
                         "sweep_deg": C._r(sweep),
                         "leaf": ({"segment": [[C._r(leaf["a"][0]), C._r(leaf["a"][1])],
                                               [C._r(leaf["b"][0]), C._r(leaf["b"][1])]],
                                   "length_mm": C._r(leaf["len"])} if leaf else None),
                         "swing": _swing_side(hinge, leaf, arc),
                         "host": ({"wall": near.get("wall"),
                                   "axis": near.get("axis"),
                                   "gap_mm": C._r(near.get("gap")),
                                   "thickness_mm": near.get("thickness")}
                                  if near.get("hit") else None),
                         "host_ok": bool(near.get("hit"))})
    else:
        # ---- 没有弧：推拉门 / 无弧洞口 / 图例里的直线符号
        angle = rect["angle"] if rect else 0.0
        along, across, skew = _split(entities, angle)
        span = max((row["len"] for row in along), default=0.0)
        leaves = [row for row in along
                  if row["len"] >= float(LIMITS["sliding_ratio"]) * span] if span else []
        jambs = [row for row in across
                 if row["len"] <= float(LIMITS["jamb_max_ratio"]) * thickness]
        axis_angle = angle
        hosts = []
        for row in along[:8]:
            hit = _wall_hit(walls, row["mid"], tol=scales["noise"] * 4.0)
            if hit.get("hit"):
                hosts.append(hit)
        host = hosts[0] if hosts else {"hit": False}
        if len(leaves) >= 2 and host.get("hit"):
            subtype = "sliding_door"
            how.append({"rule": "PARALLEL_LEAVES", "leaves": len(leaves)})
            hinge = None
            opening = _project_opening(box, host.get("axis"))
        elif jambs and host.get("hit"):
            subtype = "cased_opening"
            how.append({"rule": "JAMB_LINES", "jambs": len(jambs)})
            opening = _project_opening(box, host.get("axis"))
        else:
            negatives.append({"code": "NO_ARC_NO_JAMB"})
        geometry.update({"host": ({"wall": host.get("wall"), "axis": host.get("axis"),
                                   "opening": opening,
                                   "thickness_mm": host.get("thickness")}
                                  if host.get("hit") else None),
                         "host_ok": bool(host.get("hit")),
                         "leaves": len(leaves), "jambs": len(jambs),
                         "axis_deg": C._r(axis_angle, 2)})
        if rect:
            geometry["opening"] = opening or rect["outline"]

    # ---- 门联窗：同一洞口旁边紧贴一扇窗（由上层把同族窗候选传进来）
    with_window = None
    for sib in siblings or ():
        if not isinstance(sib, dict) or sib.get("category") != "window":
            continue
        gap = C._box_gap(box, sib.get("bbox"))
        if gap <= max(scales["noise"] * 6.0, thickness * 1.5):
            with_window = sib.get("id") or sib.get("uids")
            break
    if with_window and subtype == "swing_door":
        subtype = "door_with_window"
        how.append({"rule": "WINDOW_SIBLING", "window": with_window})

    if opening:
        width = C._len(opening[0], opening[1])
    else:
        width = float(rect["w"]) if rect else 0.0
    if width and width < float(LIMITS["min_width_ratio"]) * door_w:
        negatives.append({"code": "OPENING_TOO_NARROW", "detail": C._r(width)})
    geometry.update({
        "axis": opening,
        "opening": opening,
        "width_mm": C._r(width),
        "outline": (rect or {}).get("outline"),
        "with_window": with_window,
    })

    score = 0.58
    score += 0.16 if geometry.get("host_ok") else 0.0
    score += 0.12 if subtype in ("swing_door", "double_leaf_door") and leaf else 0.0
    score += 0.06 if opening else 0.0
    score -= 0.14 * len(negatives)
    return {"category": "door", "subtype": subtype, "how": how,
            "geometry": geometry, "negatives": negatives,
            "scales": {k: scales[k] for k in ("span", "depth", "noise", "wall")},
            "score": C._r(max(0.05, min(0.97, score)), 3),
            "status": "candidate" if len(negatives) < 2 else "uncertain",
            "evidence": {"fitter": "door", "rule": [h["rule"] for h in how],
                         "host_ok": bool(geometry.get("host_ok")),
                         "negatives": len(negatives),
                         "grid": C.grid_context(grid, box,
                                                tol=scales["noise"] * 8.0)}}


def _split(entities, angle: float):
    along: list = []
    across: list = []
    skew: list = []
    for e in entities or ():
        for a, b in C.segments_of(e):
            length = C._len(a, b)
            if length <= 0:
                continue
            direction = C._angle(a, b)
            row = {"a": a, "b": b, "len": length, "angle": direction,
                   "mid": [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]}
            if C._angle_diff(direction, angle) <= 3.0:
                along.append(row)
            elif C._angle_diff(direction, angle + 90.0) <= 3.0:
                across.append(row)
            else:
                skew.append(row)
    return along, across, skew


def _swing_side(hinge, leaf, arc):
    """开门方向：用门扇与弧端点的叉积符号（左开/右开），只作形态描述。"""
    if not hinge or not leaf:
        return None
    geom = arc.get("geom") or {}
    center = geom.get("center") or [0.0, 0.0]
    radius = float(geom.get("radius") or 0.0)
    end = float(geom.get("end_deg") or 0.0)
    arc_end = [center[0] + radius * math.cos(math.radians(end)),
               center[1] + radius * math.sin(math.radians(end))]
    v1 = [leaf["free_end"][0] - hinge[0], leaf["free_end"][1] - hinge[1]]
    v2 = [arc_end[0] - hinge[0], arc_end[1] - hinge[1]]
    cross = v1[0] * v2[1] - v1[1] * v2[0]
    return "left" if cross >= 0 else "right"


def _project_opening(box, wall_axis):
    if not box or not wall_axis:
        return None
    ang = math.radians(C._angle(wall_axis[0], wall_axis[1]))
    dx, dy = math.cos(ang), math.sin(ang)
    corners = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
    ts = [(pt[0] - wall_axis[0][0]) * dx + (pt[1] - wall_axis[0][1]) * dy
          for pt in corners]
    return [[C._r(wall_axis[0][0] + dx * min(ts)),
             C._r(wall_axis[0][1] + dy * min(ts))],
            [C._r(wall_axis[0][0] + dx * max(ts)),
             C._r(wall_axis[0][1] + dy * max(ts))]]


__all__ = ["LIMITS", "fit_instance"]
